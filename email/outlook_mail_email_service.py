from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urljoin

from core.logging_config import mask_email
from core.http_service import HttpService
from email.email_service import (
    EmailAccount,
    EmailMessage,
    EmailService,
    EmailServiceError,
)

VERIFICATION_CODE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")
HTML_SCRIPT_STYLE_PATTERN = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
OPENAI_SENDER_KEYWORD = "openai.com"
OPENAI_SUBJECT_KEYWORDS = ("chatgpt", "openai")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutlookMailTempEmailConfig:
    provider: str
    channel_id: str
    domain: str


@dataclass(frozen=True)
class OutlookMailOutlookConfig:
    pool_group_id: int
    registered_group_id: int


@dataclass(frozen=True)
class OutlookMailEmailServiceConfig:
    base_url: str
    admin_password: str
    use_temp_email: bool = False
    temp_email: OutlookMailTempEmailConfig | None = None
    outlook: OutlookMailOutlookConfig | None = None


class OutlookMailEmailService(EmailService):
    """
    对接自部署 OutlookMail 邮箱服务。

    服务初始化时完成登录、访问 launch_url、读取 CSRF token。后续业务接口统一
    复用传入的 HTTP 服务，以便自动携带服务端设置的 cookie。
    """

    LOGIN_NEXT_URL = "/#settings"
    TEMP_EMAIL_MODE = "temp"
    OUTLOOK_EMAIL_MODE = "outlook"

    def __init__(
            self,
            config: OutlookMailEmailServiceConfig,
            http_service: HttpService | None = None,
    ) -> None:
        self._config = config
        self._http_service = http_service or HttpService()
        self._csrf_token = ""
        self._csrf_disabled = False
        self._initialize_session()
        logger.debug("OutlookMail 邮箱服务初始化完成: csrf_disabled=%s", self._csrf_disabled)

    def generate_email_address(self) -> EmailAccount:
        if self._config.use_temp_email:
            logger.info("通过 OutlookMail 临时邮箱接口生成邮箱")
            return self._generate_temp_email_address()

        logger.debug("通过 OutlookMail Outlook 邮箱池分配邮箱")
        return self._allocate_outlook_email_account()

    def search_first_email(
            self,
            email_account: EmailAccount,
            sent_after: datetime,
    ) -> EmailMessage | None:
        mode = self._read_account_mode(email_account)
        normalized_sent_after = _normalize_datetime(sent_after)
        logger.debug(
            "查询邮箱验证码邮件: email=%s, mode=%s, sent_after=%s",
            mask_email(email_account.email_address),
            mode,
            normalized_sent_after.isoformat(),
        )

        if mode == self.TEMP_EMAIL_MODE:
            summaries = self._list_temp_email_messages(email_account)
            logger.info(
                "临时邮箱邮件列表获取完成: email=%s, count=%d",
                mask_email(email_account.email_address),
                len(summaries),
            )
            matched_summary = self._find_first_summary(
                summaries,
                normalized_sent_after,
            )
            if matched_summary is None:
                logger.info("临时邮箱未匹配到 OpenAI/ChatGPT 验证邮件")
                return None
            logger.debug("临时邮箱匹配到验证邮件摘要，开始获取详情")
            return self._get_temp_email_message(email_account, matched_summary)

        if mode == self.OUTLOOK_EMAIL_MODE:
            summaries = self._list_outlook_email_messages(email_account)
            logger.debug(
                "Outlook 邮件列表获取完成: email=%s, count=%d",
                mask_email(email_account.email_address),
                len(summaries),
            )
            matched_summary = self._find_first_summary(
                summaries,
                normalized_sent_after,
            )
            if matched_summary is None:
                logger.debug("Outlook 邮箱未匹配到 OpenAI/ChatGPT 验证邮件")
                return None
            logger.info("Outlook 邮箱匹配到验证邮件摘要，开始获取详情")
            return self._get_outlook_email_message(email_account, matched_summary)

        raise EmailServiceError(f"不支持的邮箱账号模式: {mode}")

    def callback(self, email_account: EmailAccount, is_email_used: bool) -> None:
        mode = self._read_account_mode(email_account)
        logger.debug(
            "邮箱服务回调: email=%s, mode=%s, is_email_used=%s",
            mask_email(email_account.email_address),
            mode,
            is_email_used,
        )
        if mode == self.TEMP_EMAIL_MODE:
            # 临时邮箱当前没有移动或归档 API，回调只作为兼容入口保留。
            return

        if mode != self.OUTLOOK_EMAIL_MODE:
            raise EmailServiceError(f"不支持的邮箱账号模式: {mode}")

        if is_email_used:
            self._move_outlook_account_to_registered_group(email_account)

    def _initialize_session(self) -> None:
        login_payload = self._request_json(
            "POST",
            "/api/extension/login",
            json={
                "password": self._config.admin_password,
                "next": self.LOGIN_NEXT_URL,
            },
            include_csrf=False,
        )
        launch_url = _read_response_string(login_payload, "launch_url")
        logger.debug("OutlookMail 登录成功，访问 extension launch_url")
        self._request_raw("GET", launch_url, include_csrf=False)

        csrf_payload = self._request_json(
            "GET",
            "/api/csrf-token",
            include_csrf=False,
        )
        self._csrf_token = _read_response_string(csrf_payload, "csrf_token")
        self._csrf_disabled = bool(csrf_payload.get("csrf_disabled", False))

    def _generate_temp_email_address(self) -> EmailAccount:
        temp_config = self._require_temp_email_config()
        payload = self._request_json(
            "POST",
            "/api/temp-emails/generate",
            json={
                "provider": temp_config.provider,
                "channel_id": temp_config.channel_id,
                "domain": temp_config.domain,
            },
        )
        email_address = _read_response_string(payload, "email")
        logger.info("临时邮箱创建成功: email=%s", mask_email(email_address))
        return EmailAccount(
            email_address=email_address,
            attributes={
                "mode": self.TEMP_EMAIL_MODE,
                "provider": temp_config.provider,
                "channel_id": temp_config.channel_id,
                "domain": temp_config.domain,
            },
        )

    def _allocate_outlook_email_account(self) -> EmailAccount:
        outlook_config = self._require_outlook_config()
        payload = self._request_json(
            "GET",
            "/api/accounts",
            params={"group_id": outlook_config.pool_group_id},
        )
        accounts = payload.get("accounts", [])
        if not isinstance(accounts, list):
            raise EmailServiceError("OutlookMail 账号列表响应中的 accounts 必须是数组")
        if not accounts:
            raise EmailServiceError(
                f"Outlook 邮件池分组 {outlook_config.pool_group_id} 没有可用邮箱"
            )

        account = accounts[0]
        if not isinstance(account, dict):
            raise EmailServiceError("OutlookMail 账号列表包含非法账号数据")

        account_id = _read_response_id(account, "id")
        account = self._get_outlook_account_detail(account_id)
        email_address = _read_response_string(account, "email")
        logger.debug(
            "Outlook 邮箱分配成功: email=%s, account_id=%s",
            mask_email(email_address),
            account_id,
        )
        return EmailAccount(
            email_address=email_address,
            attributes={
                "mode": self.OUTLOOK_EMAIL_MODE,
                "account_id": account_id,
                "client_id": account.get("client_id"),
                "refresh_token": account.get("refresh_token"),
                "registered_group_id": outlook_config.registered_group_id,
                "raw_account": account,
            },
        )

    def _get_outlook_account_detail(self, account_id: str) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            f"/api/accounts/{quote(account_id, safe='')}",
        )
        account = payload.get("account")
        if not isinstance(account, dict):
            raise EmailServiceError("OutlookMail 账号详情响应中的 account 必须是对象")
        return account

    def _list_temp_email_messages(
            self,
            email_account: EmailAccount,
    ) -> list[dict[str, Any]]:
        email_path = quote(email_account.email_address, safe="")
        payload = self._request_json(
            "GET",
            f"/api/temp-emails/{email_path}/messages",
        )
        return _read_response_list(payload, "emails")

    def _get_temp_email_message(
            self,
            email_account: EmailAccount,
            summary: dict[str, Any],
    ) -> EmailMessage:
        message_id = _read_response_string(summary, "id")
        email_path = quote(email_account.email_address, safe="")
        message_path = quote(message_id, safe="")
        payload = self._request_json(
            "GET",
            f"/api/temp-emails/{email_path}/messages/{message_path}",
        )
        email_payload = _read_response_table(payload, "email")
        return _create_email_message(
            email_account=email_account,
            email_payload=email_payload,
            fallback_summary=summary,
            mode=self.TEMP_EMAIL_MODE,
        )

    def _list_outlook_email_messages(
            self,
            email_account: EmailAccount,
    ) -> list[dict[str, Any]]:
        email_path = quote(email_account.email_address, safe="")
        payload = self._request_json(
            "GET",
            f"/api/emails/{email_path}",
            params={"folder": "all"},
        )
        return _read_response_list(payload, "emails")

    def _get_outlook_email_message(
            self,
            email_account: EmailAccount,
            summary: dict[str, Any],
    ) -> EmailMessage:
        message_id = _read_response_string(summary, "id")
        email_path = quote(email_account.email_address, safe="")
        message_path = quote(message_id, safe="")
        payload = self._request_json(
            "GET",
            f"/api/email/{email_path}/{message_path}",
        )
        email_payload = _read_response_table(payload, "email")
        return _create_email_message(
            email_account=email_account,
            email_payload=email_payload,
            fallback_summary=summary,
            mode=self.OUTLOOK_EMAIL_MODE,
        )

    def _move_outlook_account_to_registered_group(
            self,
            email_account: EmailAccount,
    ) -> None:
        account_id = _require_account_attribute(email_account, "account_id")
        client_id = _require_account_attribute(email_account, "client_id")
        refresh_token = _require_account_attribute(email_account, "refresh_token")
        registered_group_id = _require_account_attribute(
            email_account,
            "registered_group_id",
        )

        self._request_json(
            "PUT",
            f"/api/accounts/{account_id}",
            json={
                "email": email_account.email_address,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "group_id": registered_group_id,
            },
        )
        logger.debug(
            "Outlook 邮箱已移动到已注册分组: email=%s, account_id=%s, group_id=%s",
            mask_email(email_account.email_address),
            account_id,
            registered_group_id,
        )

    def _find_first_summary(
            self,
            summaries: list[dict[str, Any]],
            sent_after: datetime,
    ) -> dict[str, Any] | None:
        matched_summaries = [
            summary
            for summary in summaries
            if _matches_email_summary(
                summary,
                sent_after,
            )
        ]
        return min(
            matched_summaries,
            key=lambda summary: _parse_email_datetime(summary),
            default=None,
        )

    def _request_json(
            self,
            method: str,
            path: str,
            *,
            include_csrf: bool = True,
            **kwargs: Any,
    ) -> dict[str, Any]:
        response = self._request_raw(method, path, include_csrf=include_csrf, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmailServiceError("OutlookMail 返回了非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise EmailServiceError("OutlookMail JSON 响应必须是对象")
        if payload.get("success") is False:
            message = payload.get("message") or payload.get("error") or "未知错误"
            raise EmailServiceError(f"OutlookMail 接口调用失败: {message}")
        return payload

    def _request_raw(
            self,
            method: str,
            path: str,
            *,
            include_csrf: bool = True,
            **kwargs: Any,
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        if include_csrf and self._csrf_token and not self._csrf_disabled:
            headers["X-CSRFToken"] = self._csrf_token

        response = self._http_service.request(
            method,
            self._build_url(path),
            headers=headers,
            **kwargs,
        )
        status_code = response.status_code
        if status_code >= 400:
            response_text = response.text
            raise EmailServiceError(
                f"OutlookMail HTTP 调用失败: {status_code} {response_text}"
            )
        return response

    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(f"{self._config.base_url.rstrip('/')}/", path.lstrip("/"))

    def _require_temp_email_config(self) -> OutlookMailTempEmailConfig:
        if self._config.temp_email is None:
            raise EmailServiceError("启用临时邮箱时必须配置 temp_email")
        return self._config.temp_email

    def _require_outlook_config(self) -> OutlookMailOutlookConfig:
        if self._config.outlook is None:
            raise EmailServiceError("使用 Outlook 邮箱池时必须配置 outlook")
        return self._config.outlook

    def _read_account_mode(self, email_account: EmailAccount) -> str:
        mode = email_account.get_attribute("mode")
        if not isinstance(mode, str) or not mode:
            raise EmailServiceError("邮箱账号缺少 mode 属性，无法判断接口类型")
        return mode


def create_outlook_mail_email_service_config(
        provider_config: dict[str, Any],
) -> OutlookMailEmailServiceConfig:
    temp_email_config = _read_optional_temp_email_config(provider_config)
    outlook_config = _read_optional_outlook_config(provider_config)
    config = OutlookMailEmailServiceConfig(
        base_url=_read_required_string(provider_config, "base_url"),
        admin_password=_read_required_string(provider_config, "admin_password"),
        use_temp_email=_read_bool(provider_config, "use_temp_email", False),
        temp_email=temp_email_config,
        outlook=outlook_config,
    )

    if config.use_temp_email and config.temp_email is None:
        raise ValueError(
            "启用临时邮箱时必须配置 "
            "[email_service.providers.outlook_mail.temp_email]"
        )
    if not config.use_temp_email and config.outlook is None:
        raise ValueError(
            "使用 Outlook 邮箱池时必须配置 "
            "[email_service.providers.outlook_mail.outlook]"
        )

    return config


def _read_optional_temp_email_config(
        provider_config: dict[str, Any],
) -> OutlookMailTempEmailConfig | None:
    temp_email_config = _read_optional_table(provider_config, "temp_email")
    if temp_email_config is None:
        return None

    temp_provider = _read_required_string(temp_email_config, "provider")
    if temp_provider != "cloudflare":
        raise ValueError(f"当前仅支持 cloudflare 临时邮箱，实际配置: {temp_provider}")

    return OutlookMailTempEmailConfig(
        provider=temp_provider,
        channel_id=_read_required_string(temp_email_config, "channel_id"),
        domain=_read_required_string(temp_email_config, "domain"),
    )


def _read_optional_outlook_config(
        provider_config: dict[str, Any],
) -> OutlookMailOutlookConfig | None:
    outlook_config = _read_optional_table(provider_config, "outlook")
    if outlook_config is None:
        return None

    return OutlookMailOutlookConfig(
        pool_group_id=_read_required_int(outlook_config, "pool_group_id"),
        registered_group_id=_read_required_int(outlook_config, "registered_group_id"),
    )


def _create_email_message(
        *,
        email_account: EmailAccount,
        email_payload: dict[str, Any],
        fallback_summary: dict[str, Any],
        mode: str,
) -> EmailMessage:
    merged_payload = {**fallback_summary, **email_payload}
    message_id = _read_response_string(merged_payload, "id")
    body = str(merged_payload.get("body", merged_payload.get("body_preview", "")))
    return EmailMessage(
        email_address=email_account.email_address,
        sender=str(merged_payload.get("from", "")),
        subject=str(merged_payload.get("subject", "")),
        sent_at=_parse_email_datetime(merged_payload),
        body=body,
        message_id=message_id,
        body_type=str(merged_payload.get("body_type", "text")),
        verification_code=_extract_verification_code(body),
        attributes={
            "mode": mode,
            "raw": merged_payload,
        },
    )


def _matches_email_summary(
        summary: dict[str, Any],
        sent_after: datetime,
) -> bool:
    sent_at = _parse_email_datetime(summary)
    return (
            sent_at >= sent_after
            and OPENAI_SENDER_KEYWORD in str(summary.get("from", "")).lower()
            and _subject_matches_openai(str(summary.get("subject", "")))
    )


def _subject_matches_openai(subject: str) -> bool:
    normalized_subject = subject.lower()
    return any(keyword in normalized_subject for keyword in OPENAI_SUBJECT_KEYWORDS)


def _extract_verification_code(content: str) -> str | None:
    cleaned_content = _clean_email_content(content)
    matched_code = VERIFICATION_CODE_PATTERN.search(cleaned_content)
    if matched_code is None:
        return None
    return matched_code.group(0)


def _clean_email_content(content: str) -> str:
    without_script_style = HTML_SCRIPT_STYLE_PATTERN.sub(" ", content)
    without_tags = HTML_TAG_PATTERN.sub(" ", without_script_style)
    unescaped_content = html.unescape(without_tags)
    return WHITESPACE_PATTERN.sub(" ", unescaped_content).strip()


def _parse_email_datetime(payload: Mapping[str, Any]) -> datetime:
    for key in ("timestamp", "date", "sent_at"):
        value = payload.get(key)
        if value is not None and value != "":
            return _parse_datetime(value)
    raise EmailServiceError("邮件数据缺少可解析的发送时间")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        stripped_value = value.strip()
        if stripped_value.isdigit():
            return datetime.fromtimestamp(int(stripped_value), tz=UTC)
        iso_value = stripped_value.replace("Z", "+00:00")
        try:
            return _normalize_datetime(datetime.fromisoformat(iso_value))
        except ValueError as exc:
            raise EmailServiceError(f"无法解析邮件时间: {value}") from exc
    raise EmailServiceError(f"无法解析邮件时间类型: {type(value).__name__}")


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_account_attribute(email_account: EmailAccount, key: str) -> Any:
    value = email_account.get_attribute(key)
    if value is None or value == "":
        raise EmailServiceError(
            f"邮箱账号 {email_account.email_address} 缺少 {key}，无法完成回调"
        )
    return value


def _read_response_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise EmailServiceError(f"OutlookMail 响应缺少字符串字段: {key}")
    return value


def _read_response_id(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or value is None:
        raise EmailServiceError(f"OutlookMail 响应缺少 ID 字段: {key}")
    if isinstance(value, int | str):
        string_value = str(value).strip()
        if string_value:
            return string_value
    raise EmailServiceError(f"OutlookMail 响应 ID 字段 {key} 必须是字符串或整数")


def _read_response_list(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise EmailServiceError(f"OutlookMail 响应字段 {key} 必须是数组")
    for item in value:
        if not isinstance(item, dict):
            raise EmailServiceError(f"OutlookMail 响应字段 {key} 包含非法对象")
    return value


def _read_response_table(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise EmailServiceError(f"OutlookMail 响应字段 {key} 必须是对象")
    return value


def _read_optional_table(config: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"邮箱服务配置项 {key} 必须是 TOML 表")
    return value


def _read_required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or value == "":
        raise TypeError(f"邮箱服务配置项 {key} 必须是非空字符串")
    return value


def _read_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"邮箱服务配置项 {key} 必须是布尔值")
    return value


def _read_required_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"邮箱服务配置项 {key} 必须是整数")
    return value
