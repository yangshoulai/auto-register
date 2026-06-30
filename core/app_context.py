from __future__ import annotations

import logging
from dataclasses import dataclass

from account_export.account_export_service import (
    AccountExportService,
    create_account_export_service,
)
from account.account_service import AccountService
from core.config import AppConfig, settings
from core.http_service import HttpService
from email.email_service import EmailService, create_email_service
from sms.sms_service import SmsService, create_sms_service


logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """
    应用运行上下文。

    业务流程只需要传入这个上下文，就可以访问配置和已初始化的服务实例。
    """

    config: AppConfig
    http_service: HttpService
    account_service: AccountService
    account_export_service: AccountExportService
    email_service: EmailService
    sms_service: SmsService | None = None

    def close(self) -> None:
        logger.info("关闭应用上下文")
        self.http_service.close()

    def __enter__(self) -> AppContext:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def create_app_context(
        config: AppConfig | None = None,
        *,
        http_service: HttpService | None = None,
        sms_service: SmsService | None = None,
) -> AppContext:
    """
    创建应用上下文，并在启动阶段初始化所有当前必需服务。
    """
    app_config = config or settings
    logger.info("创建 HTTP 服务: timeout=%s, proxy=%s", app_config.http_service.default_timeout, app_config.http_service.proxy_url or "")
    resolved_http_service = http_service or HttpService(
        default_timeout=app_config.http_service.default_timeout,
        default_headers=app_config.http_service.default_headers,
        proxy_url=app_config.http_service.proxy_url,
    )

    logger.info("创建业务服务: email_provider=%s, sms_provider=%s, account_export_provider=%s",
        app_config.email_service.provider,
        app_config.sms_service.provider or "",
        app_config.account_export_service.provider,
    )

    return AppContext(
        config=app_config,
        http_service=resolved_http_service,
        account_service=AccountService(app_config.account_service),
        account_export_service=create_account_export_service(
            app_config.account_export_service,
            http_service=resolved_http_service,
        ),
        email_service=create_email_service(
            app_config.email_service,
            http_service=resolved_http_service,
        ),
        sms_service=sms_service
        if sms_service is not None
        else create_sms_service(
            app_config.sms_service,
            http_service=resolved_http_service,
        ),
    )
