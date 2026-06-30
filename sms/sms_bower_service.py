from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, sleep
from typing import Any

from core.logging_config import format_duration, mask_phone
from core.http_service import HttpService
from sms.sms_service import SmsMobileNumber, SmsService, SmsServiceError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmsBowerServiceConfig:
    base_url: str
    api_key: str
    country_id: int
    max_price: float
    min_price: float = 0
    verification_code_wait_timeout: float = 60


class SmsBowerService(SmsService):
    """
    对接 SMSBower手机号激活接口。
    """

    PROVIDER = "sms_bower"
    SERVICE_CODE = "dr"
    CANCEL_STATUS = 8
    POLL_INTERVAL_SECONDS = 5

    def __init__(
        self,
        config: SmsBowerServiceConfig,
        http_service: HttpService | None = None,
        *,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = sleep,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("SMSBower 轮询间隔必须大于 0")
        self._config = config
        self._http_service = http_service or HttpService()
        self._poll_interval_seconds = poll_interval_seconds
        self._sleeper = sleeper
        self._monotonic_clock = monotonic_clock
        logger.info(
            "SMSBower 服务已初始化: country_id=%s, min_price=%s, max_price=%s, code_timeout=%s",
            config.country_id,
            config.min_price,
            config.max_price,
            format_duration(config.verification_code_wait_timeout),
        )

    def get_mobile_number(self) -> SmsMobileNumber:
        logger.info("SMSBower 申请手机号")
        payload = self._request_json(
            {
                "api_key": self._config.api_key,
                "action": "getNumberV2",
                "service": self.SERVICE_CODE,
                "country": self._config.country_id,
                "maxPrice": self._config.max_price,
                "minPrice": self._config.min_price,
            }
        )
        activation_id = _read_response_string(payload, "activationId")
        mobile_number = _read_response_string(payload, "phoneNumber")
        logger.info(
            "SMSBower 手机号申请成功: mobile=%s, activation_id=%s, cost=%s",
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
                "country_code": payload.get("countryCode"),
                "can_get_another_sms": payload.get("canGetAnotherSms"),
                "activation_time": payload.get("activationTime"),
                "activation_operator": payload.get("activationOperator"),
                "raw": payload,
            },
        )

    def get_latest_verification_code(
        self,
        mobile_number: SmsMobileNumber,
        sent_after: datetime,
    ) -> str | None:
        _ = sent_after
        activation_id = _require_mobile_attribute(mobile_number, "activation_id")
        deadline = self._monotonic_clock() + self._config.verification_code_wait_timeout
        poll_count = 0

        while True:
            poll_count += 1
            logger.info(
                "SMSBower 查询验证码: mobile=%s, activation_id=%s, poll=%d",
                mask_phone(mobile_number.mobile_number),
                activation_id,
                poll_count,
            )
            code = self._query_latest_verification_code(activation_id)
            if code:
                logger.info(
                    "SMSBower 已获取验证码: mobile=%s, poll=%d",
                    mask_phone(mobile_number.mobile_number),
                    poll_count,
                )
                return code

            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds <= 0:
                logger.warning(
                    "SMSBower 等待验证码超时: mobile=%s, timeout=%s",
                    mask_phone(mobile_number.mobile_number),
                    format_duration(self._config.verification_code_wait_timeout),
                )
                return None

            self._sleeper(min(self._poll_interval_seconds, remaining_seconds))

    def _query_latest_verification_code(self, activation_id: str) -> str | None:
        status_text = self._request_text(
            {
                "api_key": self._config.api_key,
                "action": "getStatus",
                "id": activation_id,
            }
        )
        return _extract_verification_code(status_text)

    def callback(
        self,
        mobile_number: SmsMobileNumber,
        is_verification_code_received: bool,
    ) -> None:
        if is_verification_code_received:
            logger.info(
                "SMSBower 回调: 已收到验证码，不取消激活: mobile=%s",
                mask_phone(mobile_number.mobile_number),
            )
            return

        try:
            activation_id = _require_mobile_attribute(mobile_number, "activation_id")
            logger.warning(
                "SMSBower 回调: 未收到验证码，取消激活: mobile=%s, activation_id=%s",
                mask_phone(mobile_number.mobile_number),
                activation_id,
            )
            status_text = self._request_text(
                {
                    "api_key": self._config.api_key,
                    "action": "setStatus",
                    "status": self.CANCEL_STATUS,
                    "id": activation_id,
                }
            )
            if not status_text.startswith("ACCESS_"):
                raise SmsServiceError(f"SMSBower 取消激活失败: {status_text}")
        except Exception:
            logger.exception(
                "SMSBower 取消激活回调失败，已忽略并继续流程: mobile=%s",
                mask_phone(mobile_number.mobile_number),
            )

    def _request_json(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request_raw(params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmsServiceError("SMSBower 返回了非 JSON 响应") from exc

        if not isinstance(payload, dict):
            raise SmsServiceError("SMSBower JSON 响应必须是对象")
        if payload.get("success") is False or payload.get("error"):
            message = payload.get("message") or payload.get("error") or "未知错误"
            raise SmsServiceError(f"SMSBower 接口调用失败: {message}")
        return payload

    def _request_text(self, params: dict[str, Any]) -> str:
        response = self._request_raw(params)
        return _read_response_text(response)

    def _request_raw(self, params: dict[str, Any]) -> Any:
        response = self._http_service.request(
            "GET",
            self._config.base_url,
            params=params,
        )
        status_code = response.status_code
        if status_code >= 400:
            response_text = response.text
            raise SmsServiceError(
                f"SMSBower HTTP 调用失败: {status_code} {response_text}"
            )
        return response


def create_sms_bower_service_config(
    provider_config: dict[str, Any],
) -> SmsBowerServiceConfig:
    return SmsBowerServiceConfig(
        base_url=_read_required_string(provider_config, "base_url"),
        api_key=_read_required_string(provider_config, "api_key"),
        country_id=_read_required_int(provider_config, "country_id"),
        max_price=_read_required_float(provider_config, "max_price"),
        min_price=_read_non_negative_float(provider_config, "min_price", 0),
        verification_code_wait_timeout=_read_float(
            provider_config,
            "verification_code_wait_timeout",
            60,
        ),
    )


def _extract_verification_code(status_text: str) -> str | None:
    normalized_status = status_text.strip()
    if normalized_status == "STATUS_WAIT_CODE":
        return None
    if normalized_status == "STATUS_CANCEL":
        raise SmsServiceError("SMSBower 激活已取消")
    if normalized_status.startswith("STATUS_OK:"):
        return _read_status_code(normalized_status, "STATUS_OK:")
    if normalized_status.startswith("STATUS_WAIT_RETRY:"):
        return _read_status_code(normalized_status, "STATUS_WAIT_RETRY:")

    raise SmsServiceError(f"SMSBower 返回了未知短信状态: {status_text}")


def _read_status_code(status_text: str, prefix: str) -> str | None:
    code = status_text.removeprefix(prefix).strip()
    code = code.strip("'\" ")
    return code or None


def _read_response_text(response: Any) -> str:
    return response.text.strip()


def _require_mobile_attribute(mobile_number: SmsMobileNumber, key: str) -> str:
    value = mobile_number.get_attribute(key)
    if value is None or value == "":
        raise SmsServiceError(
            f"手机号 {mobile_number.mobile_number} 缺少 {key}，无法调用 SMSBower"
        )
    return str(value)


def _read_response_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or value is None:
        raise SmsServiceError(f"SMSBower 响应缺少字符串字段: {key}")

    string_value = str(value).strip()
    if string_value == "":
        raise SmsServiceError(f"SMSBower 响应缺少字符串字段: {key}")
    return string_value


def _read_required_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or value == "":
        raise TypeError(f"SMSBower 配置项 {key} 必须是非空字符串")
    return value


def _read_required_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool):
        raise TypeError(f"SMSBower 配置项 {key} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise TypeError(f"SMSBower 配置项 {key} 必须是整数")


def _read_required_float(config: dict[str, Any], key: str) -> float:
    value = config.get(key)
    if isinstance(value, bool):
        raise TypeError(f"SMSBower 配置项 {key} 必须是数字")
    if isinstance(value, int | float):
        float_value = float(value)
    elif isinstance(value, str):
        try:
            float_value = float(value)
        except ValueError as exc:
            raise TypeError(f"SMSBower 配置项 {key} 必须是数字") from exc
    else:
        raise TypeError(f"SMSBower 配置项 {key} 必须是数字")

    if float_value <= 0:
        raise ValueError(f"SMSBower 配置项 {key} 必须大于 0")
    return float_value


def _read_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise TypeError(f"SMSBower 配置项 {key} 必须是数字")
    if isinstance(value, int | float):
        float_value = float(value)
    elif isinstance(value, str):
        try:
            float_value = float(value)
        except ValueError as exc:
            raise TypeError(f"SMSBower 配置项 {key} 必须是数字") from exc
    else:
        raise TypeError(f"SMSBower 配置项 {key} 必须是数字")

    if float_value <= 0:
        raise ValueError(f"SMSBower 配置项 {key} 必须大于 0")
    return float_value


def _read_non_negative_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise TypeError(f"SMSBower 配置项 {key} 必须是数字")
    if isinstance(value, int | float):
        float_value = float(value)
    elif isinstance(value, str):
        try:
            float_value = float(value)
        except ValueError as exc:
            raise TypeError(f"SMSBower 配置项 {key} 必须是数字") from exc
    else:
        raise TypeError(f"SMSBower 配置项 {key} 必须是数字")

    if float_value < 0:
        raise ValueError(f"SMSBower 配置项 {key} 必须大于等于 0")
    return float_value
