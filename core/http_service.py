from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter
from typing import Any

from curl_cffi import requests

from core.logging_config import format_duration, sanitize_mapping, sanitize_url


logger = logging.getLogger(__name__)


class HttpService:
    """
    统一管理全局共享 HTTP session。

    所有外部服务通过同一个 session 访问网络，因此 Cookie、连接池和默认超时都在
    这里统一维护。
    """

    def __init__(
        self,
        *,
        default_timeout: float = 30,
        default_headers: dict[str, str] | None = None,
        proxy_url: str | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._default_headers = default_headers or {}
        self._proxy_url = proxy_url
        self._session = (session_factory or requests.Session)()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._default_timeout)

        headers = dict(self._default_headers)
        headers.update(kwargs.pop("headers", {}))
        if headers:
            kwargs["headers"] = headers

        if self._proxy_url and "proxy" not in kwargs and "proxies" not in kwargs:
            kwargs["proxy"] = self._proxy_url

        sanitized_url = sanitize_url(url)
        logger.info(
            "HTTP 请求: method=%s, url=%s, timeout=%s, params=%s, json=%s, header_keys=%s",
            method.upper(),
            sanitized_url,
            kwargs.get("timeout"),
            _sanitize_payload(kwargs.get("params")),
            _sanitize_payload(kwargs.get("json")),
            sorted((kwargs.get("headers") or {}).keys()),
        )
        started_at = perf_counter()
        try:
            response = self._session.request(method, url, **kwargs)
        except Exception:
            logger.exception(
                "HTTP 请求异常: method=%s, url=%s, elapsed=%s",
                method.upper(),
                sanitized_url,
                format_duration(perf_counter() - started_at),
            )
            raise

        logger.info(
            "HTTP 响应: method=%s, url=%s, status=%s, elapsed=%s",
            method.upper(),
            sanitized_url,
            response.status_code,
            format_duration(perf_counter() - started_at),
        )
        return response

    def close(self) -> None:
        self._session.close()


def _sanitize_payload(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return sanitize_mapping(value)
    return f"<{type(value).__name__}>"
