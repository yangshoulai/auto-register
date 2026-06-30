from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any

from core.logging_config import format_duration, mask_phone
from core.http_service import HttpService
from sms.sms_service import SmsMobileNumber, SmsService, SmsServiceError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeroSmsServiceConfig:
    base_url: str
    api_key: str
    country_id: int
    max_price: float
    verification_code_wait_timeout: float = 125


class HeroSmsService(SmsService):
    """
    对接 HeroSMS手机号激活接口。
    """

    PROVIDER = "hero_sms"
    SERVICE_CODE = "dr"
    CANCEL_STATUS = 8
    POLL_INTERVAL_SECONDS = 5

    def __init__(
        self,
        config: HeroSmsServiceConfig,
        http_service: HttpService | None = None,
        *,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = sleep,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("HeroSMS 轮询间隔必须大于 0")
        self._config = config
        self._http_service = http_service or HttpService()
        self._poll_interval_seconds = poll_interval_seconds
        self._sleeper = sleeper
        self._monotonic_clock = monotonic_clock
        logger.info(
            "HeroSMS 服务已初始化: country_id=%s, max_price=%s, code_timeout=%s",
            config.country_id,
            config.max_price,
            format_duration(config.verification_code_wait_timeout),
        )

    def get_mobile_number(self) -> SmsMobileNumber:
        logger.info("HeroSMS 申请手机号")
        payload = self._request_json(
            {
                "action": "getNumberV2",
                "service": self.SERVICE_CODE,
                "country": self._config.country_id,
                "maxPrice": self._config.max_price,
                "api_key": self._config.api_key,
            }
        )
        activation_id = _read_response_string(payload, "activationId")
        mobile_number = _read_response_string(payload, "phoneNumber")
        logger.info(
            "HeroSMS 手机号申请成功: mobile=%s, activation_id=%s, cost=%s",
            mask_phone(mobile_number),
            activation_id,
            payload.get("activationCost"),
        )

        return SmsMobileNumber(
            mobile_number=mobile_number,
            attributes={
                "provider": self.PROVIDER,
                "activation_id": activation_id,
                "activation_cost": payload.get("activationCost"),
                "currency": payload.get("currency"),
                "country_code": payload.get("countryCode"),
                "country_phone_code": payload.get("countryPhoneCode"),
                "can_get_another_sms": payload.get("canGetAnotherSms"),
                "activation_time": payload.get("activationTime"),
                "activation_end_time": payload.get("activationEndTime"),
                "activation_operator": payload.get("activationOperator"),
                "raw": payload,
            },
        )

    def get_latest_verification_code(
        self,
        mobile_number: SmsMobileNumber,
        sent_after: datetime,
    ) -> str | None:
        activation_id = _require_mobile_attribute(mobile_number, "activation_id")
        deadline = self._monotonic_clock() + self._config.verification_code_wait_timeout
        poll_count = 0

        while True:
            poll_count += 1
            logger.info(
                "HeroSMS 查询验证码: mobile=%s, activation_id=%s, poll=%d",
                mask_phone(mobile_number.mobile_number),
                activation_id,
                poll_count,
            )
            code = self._query_latest_verification_code(activation_id, sent_after)
            if code:
                logger.info(
                    "HeroSMS 已获取验证码: mobile=%s, poll=%d",
                    mask_phone(mobile_number.mobile_number),
                    poll_count,
                )
                return code

            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds <= 0:
                logger.warning(
                    "HeroSMS 等待验证码超时: mobile=%s, timeout=%s",
                    mask_phone(mobile_number.mobile_number),
                    format_duration(self._config.verification_code_wait_timeout),
                )
                return None

            self._sleeper(min(self._poll_interval_seconds, remaining_seconds))

    def _query_latest_verification_code(
        self,
        activation_id: str,
        sent_after: datetime,
    ) -> str | None:
        payload = self._request_json(
            {
                "action": "getStatusV2",
                "id": activation_id,
                "api_key": self._config.api_key,
            }
        )
        return _extract_latest_verification_code(payload, sent_after)

    def callback(
        self,
        mobile_number: SmsMobileNumber,
        is_verification_code_received: bool,
    ) -> None:
        if is_verification_code_received:
            logger.info(
                "HeroSMS 回调: 已收到验证码，不取消激活: mobile=%s",
                mask_phone(mobile_number.mobile_number),
            )
            return

        try:
            activation_id = _require_mobile_attribute(mobile_number, "activation_id")
            logger.warning(
                "HeroSMS 回调: 未收到验证码，取消激活: mobile=%s, activation_id=%s",
                mask_phone(mobile_number.mobile_number),
                activation_id,
            )
            self._request_raw(
                {
                    "action": "setStatus",
                    "id": activation_id,
                    "status": self.CANCEL_STATUS,
                    "api_key": self._config.api_key,
                }
            )
        except Exception:
            logger.exception(
                "HeroSMS 取消激活回调失败，已忽略并继续流程: mobile=%s",
                mask_phone(mobile_number.mobile_number),
            )

    def _request_json(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request_raw(params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmsServiceError("HeroSMS 返回了非 JSON 响应") from exc

        if not isinstance(payload, dict):
            raise SmsServiceError("HeroSMS JSON 响应必须是对象")
        if payload.get("success") is False or payload.get("error"):
            message = payload.get("message") or payload.get("error") or "未知错误"
            raise SmsServiceError(f"HeroSMS 接口调用失败: {message}")
        return payload

    def _request_raw(self, params: dict[str, Any]) -> Any:
        response = self._http_service.request(
            "GET",
            self._config.base_url,
            params=params,
        )
        status_code = response.status_code
        if status_code >= 400:
            response_text = response.text
            raise SmsServiceError(f"HeroSMS HTTP 调用失败: {status_code} {response_text}")
        return response


def create_hero_sms_service_config(
    provider_config: dict[str, Any],
) -> HeroSmsServiceConfig:
    return HeroSmsServiceConfig(
        base_url=_read_required_string(provider_config, "base_url"),
        api_key=_read_required_string(provider_config, "api_key"),
        country_id=_read_required_int(provider_config, "country_id"),
        max_price=_read_required_float(provider_config, "max_price"),
        verification_code_wait_timeout=_read_float(
            provider_config,
            "verification_code_wait_timeout",
            125,
        ),
    )


def _extract_latest_verification_code(
    payload: Mapping[str, Any],
    sent_after: datetime,
) -> str | None:
    normalized_sent_after = _normalize_datetime(sent_after)
    oldest_datetime = datetime.min.replace(tzinfo=UTC)
    candidates: list[tuple[str, datetime | None, int]] = []

    for priority, channel_name in ((1, "sms"), (0, "call")):
        channel_payload = payload.get(channel_name)
        if not isinstance(channel_payload, dict):
            continue

        code = _read_optional_code(channel_payload)
        if code is None:
            continue

        received_at = _parse_optional_datetime(channel_payload.get("dateTime"))
        if received_at is not None and received_at < normalized_sent_after:
            continue

        candidates.append((code, received_at, priority))

    if not candidates:
        return None

    code, _, _ = max(
        candidates,
        key=lambda candidate: (
            candidate[1] is not None,
            candidate[1] or oldest_datetime,
            candidate[2],
        ),
    )
    return code


def _read_optional_code(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("code")
    if value is None:
        return None

    code = str(value).strip()
    if not code or code.lower() == "null":
        return None
    return code


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str):
        return None

    stripped_value = value.strip()
    if stripped_value == "" or stripped_value.startswith("0000-00-00"):
        return None
    if stripped_value.isdigit():
        return datetime.fromtimestamp(int(stripped_value), tz=UTC)

    iso_value = stripped_value.replace("Z", "+00:00")
    try:
        return _normalize_datetime(datetime.fromisoformat(iso_value))
    except ValueError:
        return None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_mobile_attribute(mobile_number: SmsMobileNumber, key: str) -> str:
    value = mobile_number.get_attribute(key)
    if value is None or value == "":
        raise SmsServiceError(
            f"手机号 {mobile_number.mobile_number} 缺少 {key}，无法调用 HeroSMS"
        )
    return str(value)


def _read_response_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or value is None:
        raise SmsServiceError(f"HeroSMS 响应缺少字符串字段: {key}")

    string_value = str(value).strip()
    if string_value == "":
        raise SmsServiceError(f"HeroSMS 响应缺少字符串字段: {key}")
    return string_value


def _read_required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or value == "":
        raise TypeError(f"HeroSMS 配置项 {key} 必须是非空字符串")
    return value


def _read_required_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool):
        raise TypeError(f"HeroSMS 配置项 {key} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise TypeError(f"HeroSMS 配置项 {key} 必须是整数")


def _read_required_float(config: dict[str, Any], key: str) -> float:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"HeroSMS 配置项 {key} 必须是数字")
    if value <= 0:
        raise ValueError(f"HeroSMS 配置项 {key} 必须大于 0")
    return float(value)


def _read_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"HeroSMS 配置项 {key} 必须是数字")
    if value <= 0:
        raise ValueError(f"HeroSMS 配置项 {key} 必须大于 0")
    return float(value)
