from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from account_export.account_export_service import (
    AccountExportOauthUrl,
    AccountExportService,
    AccountExportServiceError,
    AccountExportSubmitResult,
)
from core.logging_config import sanitize_url
from core.http_service import HttpService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CpaAccountExportServiceConfig:
    base_url: str
    secret_key: str


class CpaAccountExportService(AccountExportService):
    """
    对接 CPA 管理接口，导出 Codex OAuth 授权结果。
    """

    PROVIDER = "cpa"
    MANAGEMENT_KEY_HEADER = "X-Management-Key"
    DEFAULT_BASE_URL = "http://localhost:8317/v0/management"

    def __init__(
        self,
        config: CpaAccountExportServiceConfig,
        http_service: HttpService | None = None,
    ) -> None:
        self._config = config
        self._http_service = http_service or HttpService()

    def get_oauth_url(self) -> AccountExportOauthUrl:
        logger.info("CPA 获取 Codex OAuth 链接")
        payload = self._request_json(
            "GET",
            "/codex-auth-url",
            params={"is_webui": "true"},
        )
        if payload.get("status") != "ok":
            raise AccountExportServiceError(
                f"CPA 获取 OAuth 链接失败: {payload.get('error') or payload}"
            )

        logger.info("CPA OAuth 链接获取成功: %s", _read_response_string(payload, "url") or "")
        return AccountExportOauthUrl(
            url=_read_response_string(payload, "url"),
            state=_read_optional_string(payload, "state"),
            attributes={
                "provider": self.PROVIDER,
                "raw": payload,
            },
        )

    def submit_redirect_url(self, redirect_url: str) -> AccountExportSubmitResult:
        logger.debug("CPA 提交 OAuth 回调地址: redirect_url=%s", sanitize_url(redirect_url))
        payload = self._request_json(
            "POST",
            "/oauth-callback",
            json={
                "provider": "codex",
                "redirect_url": redirect_url,
            },
        )
        status = str(payload.get("status", ""))
        error = payload.get("error")
        logger.debug(
            "CPA OAuth 回调提交完成: status=%s, error=%s",
            status,
            error or "",
        )
        return AccountExportSubmitResult(
            success=status == "ok",
            status=status,
            error=str(error) if error is not None else None,
            attributes={
                "provider": self.PROVIDER,
                "raw": payload,
            },
        )

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http_service.request(
            method,
            self._build_url(path),
            headers={self.MANAGEMENT_KEY_HEADER: self._config.secret_key},
            **kwargs,
        )
        status_code = response.status_code
        if status_code >= 400:
            response_text = response.text
            raise AccountExportServiceError(
                f"CPA HTTP 调用失败: {status_code} {response_text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AccountExportServiceError("CPA 返回了非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise AccountExportServiceError("CPA JSON 响应必须是对象")
        return payload

    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(f"{self._config.base_url.rstrip('/')}/", path.lstrip("/"))


def create_cpa_account_export_service_config(
    provider_config: dict[str, Any],
) -> CpaAccountExportServiceConfig:
    return CpaAccountExportServiceConfig(
        base_url=_read_optional_config_string(
            provider_config,
            "base_url",
            CpaAccountExportService.DEFAULT_BASE_URL,
        ),
        secret_key=_read_required_string(provider_config, "secret_key"),
    )


def _read_optional_config_string(config: dict[str, Any], key: str, default: str) -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"CPA 账号导出配置 {key} 必须是字符串")
    if value == "":
        return default
    return value


def _read_required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"CPA 账号导出配置 {key} 必须是非空字符串")
    return value


def _read_response_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AccountExportServiceError(f"CPA 响应缺少字段: {key}")
    return value


def _read_optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AccountExportServiceError(f"CPA 响应字段 {key} 必须是字符串")
    return value
