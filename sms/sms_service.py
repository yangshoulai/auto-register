from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.config import PROJECT_ROOT, SmsActivationStoreConfig, SmsServiceConfig, settings
from core.http_service import HttpService
from sms.activation_store import SmsActivationStore


class SmsServiceError(RuntimeError):
    """
    短信服务调用失败。
    """


@dataclass(frozen=True)
class SmsMobileNumber:
    """
    通用手机号模型。

    mobile_number 是所有短信服务都必须提供的手机号；attributes 用于保存
    服务商返回的激活编号、订单 ID、项目 ID 等动态属性。
    """

    mobile_number: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def get_attribute(self, key: str, default: Any = None) -> Any:
        return self.attributes.get(key, default)


class SmsService(ABC):
    @abstractmethod
    def get_mobile_number(
        self,
        excluded_activation_ids: Collection[str] | None = None,
    ) -> SmsMobileNumber:
        """
        从短信服务商获取一个新的手机号。

        excluded_activation_ids 用于排除本轮注册已经尝试过的激活，避免重试时
        反复拿到同一个手机号。
        """

    @abstractmethod
    def get_latest_verification_code(
        self,
        mobile_number: SmsMobileNumber,
        sent_after: datetime,
    ) -> str | None:
        """
        按手机号和短信发送时间下限，等待并获取最新一次短信验证码。

        具体轮询间隔和等待超时由各短信服务实现自己的配置控制。
        """

    @abstractmethod
    def callback(
        self,
        mobile_number: SmsMobileNumber,
        is_verification_code_received: bool,
    ) -> None:
        """
        手机号使用结果回调。

        当未收到验证码时，具体服务可在这里取消服务商交易，避免继续计费。
        """


def _build_hero_sms_service(
    provider_config: dict[str, Any],
    http_service: HttpService | None,
    activation_store: SmsActivationStore | None,
    activation_store_config: SmsActivationStoreConfig,
) -> SmsService:
    from sms.hero_sms_service import (
        HeroSmsService,
        create_hero_sms_service_config,
    )

    return HeroSmsService(
        create_hero_sms_service_config(provider_config),
        http_service=http_service,
        activation_store=activation_store,
        activation_store_config=activation_store_config,
    )


def _build_sms_bower_service(
    provider_config: dict[str, Any],
    http_service: HttpService | None,
    activation_store: SmsActivationStore | None,
    activation_store_config: SmsActivationStoreConfig,
) -> SmsService:
    from sms.sms_bower_service import (
        SmsBowerService,
        create_sms_bower_service_config,
    )

    return SmsBowerService(
        create_sms_bower_service_config(provider_config),
        http_service=http_service,
        activation_store=activation_store,
        activation_store_config=activation_store_config,
    )


SmsServiceBuilder = Callable[
    [
        dict[str, Any],
        HttpService | None,
        SmsActivationStore | None,
        SmsActivationStoreConfig,
    ],
    SmsService,
]


SMS_SERVICE_BUILDERS: dict[str, SmsServiceBuilder] = {
    "hero_sms": _build_hero_sms_service,
    "sms_bower": _build_sms_bower_service,
    "smsbower": _build_sms_bower_service,
}


def create_sms_service(
    config: SmsServiceConfig | None = None,
    *,
    http_service: HttpService | None = None,
) -> SmsService | None:
    sms_config = config or settings.sms_service
    if sms_config.provider is None:
        return None

    service_builder = SMS_SERVICE_BUILDERS.get(sms_config.provider)
    if service_builder is None:
        supported_providers = ", ".join(sorted(SMS_SERVICE_BUILDERS))
        raise ValueError(
            f"不支持的短信服务提供者: {sms_config.provider}，"
            f"当前支持: {supported_providers}"
        )

    activation_store = SmsActivationStore(
        _resolve_sqlite_path(sms_config.activation_store.sqlite_path)
    )
    return service_builder(
        sms_config.provider_config,
        http_service,
        activation_store,
        sms_config.activation_store,
    )


def _resolve_sqlite_path(sqlite_path: str) -> str:
    path = PROJECT_ROOT / sqlite_path
    if sqlite_path.startswith("/"):
        return sqlite_path
    return str(path)
