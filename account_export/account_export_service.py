from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.config import AccountExportServiceConfig, settings
from core.http_service import HttpService


class AccountExportServiceError(RuntimeError):
    """
    账号导出服务调用失败。
    """


@dataclass(frozen=True)
class AccountExportOauthUrl:
    url: str
    state: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountExportSubmitResult:
    success: bool
    status: str
    error: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


class AccountExportService(ABC):
    @abstractmethod
    def get_oauth_url(self) -> AccountExportOauthUrl:
        """
        获取 OAuth 授权链接。
        """

    @abstractmethod
    def submit_redirect_url(self, redirect_url: str) -> AccountExportSubmitResult:
        """
        提交 OAuth 重定向回调链接。
        """


AccountExportServiceBuilder = Callable[
    [dict[str, Any], HttpService | None],
    AccountExportService,
]


def _build_cpa_account_export_service(
    provider_config: dict[str, Any],
    http_service: HttpService | None,
) -> AccountExportService:
    from account_export.cpa_account_export_service import (
        CpaAccountExportService,
        create_cpa_account_export_service_config,
    )

    return CpaAccountExportService(
        create_cpa_account_export_service_config(provider_config),
        http_service=http_service,
    )


ACCOUNT_EXPORT_SERVICE_BUILDERS: dict[str, AccountExportServiceBuilder] = {
    "cpa": _build_cpa_account_export_service,
}


def create_account_export_service(
    config: AccountExportServiceConfig | None = None,
    *,
    http_service: HttpService | None = None,
) -> AccountExportService:
    export_config = config or settings.account_export_service
    service_builder = ACCOUNT_EXPORT_SERVICE_BUILDERS.get(export_config.provider)
    if service_builder is None:
        supported_providers = ", ".join(sorted(ACCOUNT_EXPORT_SERVICE_BUILDERS))
        raise ValueError(
            f"不支持的账号导出服务提供者: {export_config.provider}，"
            f"当前支持: {supported_providers}"
        )

    return service_builder(export_config.provider_config, http_service)
