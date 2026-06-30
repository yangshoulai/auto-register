from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.config import EmailServiceConfig, settings
from core.http_service import HttpService


class EmailServiceError(RuntimeError):
    """
    邮箱服务调用失败。
    """


@dataclass(frozen=True)
class EmailAccount:
    """
    通用邮箱模型。

    email_address 是所有邮箱服务都必须提供的稳定地址；attributes 用于保存
    第三方服务返回的账号 ID、客户端 ID、模式等动态属性。
    """

    email_address: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def get_attribute(self, key: str, default: Any = None) -> Any:
        return self.attributes.get(key, default)


@dataclass(frozen=True)
class EmailMessage:
    email_address: str
    sender: str
    subject: str
    sent_at: datetime
    body: str = ""
    message_id: str | None = None
    body_type: str = "text"
    verification_code: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


class EmailService(ABC):
    @abstractmethod
    def generate_email_address(self) -> EmailAccount:
        """
        生成或分配一个邮箱账号。
        """

    @abstractmethod
    def search_first_email(
            self,
            email_account: EmailAccount,
            sent_after: datetime,
    ) -> EmailMessage | None:
        """
        按邮箱账号和发送时间下限搜索第一封 OpenAI/ChatGPT 验证邮件。
        """

    @abstractmethod
    def callback(self, email_account: EmailAccount, is_email_used: bool) -> None:
        """
        邮箱使用结果回调。
        """


EmailServiceBuilder = Callable[[dict[str, Any], HttpService | None], EmailService]


def _build_outlook_mail_email_service(
        provider_config: dict[str, Any],
        http_service: HttpService | None,
) -> EmailService:
    from email.outlook_mail_email_service import (
        OutlookMailEmailService,
        create_outlook_mail_email_service_config,
    )

    return OutlookMailEmailService(
        create_outlook_mail_email_service_config(provider_config),
        http_service=http_service,
    )


EMAIL_SERVICE_BUILDERS: dict[str, EmailServiceBuilder] = {
    "outlook_mail": _build_outlook_mail_email_service,
}


def create_email_service(
        config: EmailServiceConfig | None = None,
        *,
        http_service: HttpService | None = None,
) -> EmailService:
    email_config = config or settings.email_service
    service_builder = EMAIL_SERVICE_BUILDERS.get(email_config.provider)
    if service_builder is None:
        supported_providers = ", ".join(sorted(EMAIL_SERVICE_BUILDERS))
        raise ValueError(
            f"不支持的邮箱服务提供者: {email_config.provider}，"
            f"当前支持: {supported_providers}"
        )

    return service_builder(email_config.provider_config, http_service)
