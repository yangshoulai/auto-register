from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from typing import Any

from core.config import SmsActivationStoreConfig
from core.http_service import HttpService
from core.logging_config import format_duration, mask_phone
from sms.activation_store import (
    SmsActivationRecord,
    SmsActivationStore,
    VerificationCodeEntry,
)
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
    activation_valid_seconds: float = 1500


@dataclass(frozen=True)
class SmsBowerVerificationCodeResult:
    code: str
    text: str = ""
    raw: str = ""


class SmsBowerService(SmsService):
    """
    对接 SMSBower 手机号激活接口。
    """

    PROVIDER = "sms_bower"
    SERVICE_CODE = "dr"
    CANCEL_STATUS = 8
    REQUEST_NEW_SMS_STATUS = 3
    REQUEST_NEW_SMS_SUCCESS_STATUSES = {"ACCESS_READY", "ACCESS_RETRY_GET"}
    POLL_INTERVAL_SECONDS = 5

    def __init__(
            self,
            config: SmsBowerServiceConfig,
            http_service: HttpService | None = None,
            *,
            activation_store: SmsActivationStore | None = None,
            activation_store_config: SmsActivationStoreConfig | None = None,
            poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
            sleeper: Callable[[float], None] = sleep,
            monotonic_clock: Callable[[], float] = monotonic,
            now: Callable[[], datetime] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("SMSBower 轮询间隔必须大于 0")
        self._config = config
        self._http_service = http_service or HttpService()
        self._activation_store = activation_store
        self._activation_store_config = (
            activation_store_config or SmsActivationStoreConfig()
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._sleeper = sleeper
        self._monotonic_clock = monotonic_clock
        self._now = now or (lambda: datetime.now(UTC))
        logger.info(
            "SMSBower 服务已初始化: country_id=%s, min_price=%s, max_price=%s, "
            "code_timeout=%s, activation_valid=%s, reuse_local=%s",
            config.country_id,
            config.min_price,
            config.max_price,
            format_duration(config.verification_code_wait_timeout),
            format_duration(config.activation_valid_seconds),
            self._activation_store_config.reuse_local_activation,
        )

    def get_mobile_number(
            self,
            excluded_activation_ids: Collection[str] | None = None,
    ) -> SmsMobileNumber:
        if (
            self._activation_store is not None
            and self._activation_store_config.reuse_local_activation
        ):
            reused_mobile_number = self._get_reusable_local_mobile_number(
                excluded_activation_ids,
            )
            if reused_mobile_number is not None:
                return reused_mobile_number

        logger.info("SMSBower 申请新手机号")
        return self._request_new_mobile_number()

    def _get_reusable_local_mobile_number(
            self,
            excluded_activation_ids: Collection[str] | None,
    ) -> SmsMobileNumber | None:
        if self._activation_store is None:
            return None

        records = self._activation_store.list_reusable_activations(
            provider=self.PROVIDER,
            service_code=self.SERVICE_CODE,
            excluded_activation_ids=excluded_activation_ids,
            now=self._now(),
            reuse_min_interval_seconds=(
                self._activation_store_config.reuse_min_interval_seconds
            ),
            min_remaining_seconds=self._config.verification_code_wait_timeout,
        )
        for record in records:
            try:
                logger.info(
                    "SMSBower 尝试复用本地激活: mobile=%s, activation_id=%s, end_time=%s",
                    mask_phone(record.mobile_number),
                    record.activation_id,
                    record.activation_end_time.isoformat(),
                )
                self._request_new_sms_for_activation(record.activation_id)
            except Exception as exc:
                logger.exception(
                    "SMSBower 本地激活请求新验证码失败，标记不可用: activation_id=%s",
                    record.activation_id,
                )
                self._mark_activation_unavailable(record.activation_id, str(exc))
                continue

            logger.info(
                "SMSBower 本地激活复用成功: mobile=%s, activation_id=%s",
                mask_phone(record.mobile_number),
                record.activation_id,
            )
            return _create_mobile_number_from_record(record, reused_activation=True)

        return None

    def _request_new_mobile_number(self) -> SmsMobileNumber:
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
        activation_time = _read_response_datetime(payload, "activationTime")
        activation_end_time = activation_time + timedelta(
            seconds=self._config.activation_valid_seconds,
        )
        logger.info(
            "SMSBower 新手机号申请成功: mobile=%s, activation_id=%s, cost=%s",
            mask_phone(mobile_number),
            activation_id,
            payload.get("activationCost"),
        )

        record = SmsActivationRecord(
            provider=self.PROVIDER,
            service_code=self.SERVICE_CODE,
            mobile_number=mobile_number,
            activation_id=activation_id,
            activation_cost=payload.get("activationCost"),
            country_code=payload.get("countryCode"),
            activation_operator=payload.get("activationOperator"),
            activation_time=activation_time,
            activation_end_time=activation_end_time,
            can_get_another_sms=_is_enabled_flag(payload.get("canGetAnotherSms")),
            raw=payload,
        )
        if self._activation_store is not None:
            self._activation_store.upsert_activation(record)
        return _create_mobile_number_from_record(record, reused_activation=False)

    def _request_new_sms_for_activation(self, activation_id: str) -> None:
        status_text = self._request_text(
            {
                "api_key": self._config.api_key,
                "action": "setStatus",
                "status": self.REQUEST_NEW_SMS_STATUS,
                "id": activation_id,
            }
        )
        if status_text not in self.REQUEST_NEW_SMS_SUCCESS_STATUSES:
            raise SmsServiceError(f"SMSBower 请求新验证码失败: {status_text}")

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
            result = self._query_latest_verification_code(activation_id)
            if result is not None:
                self._record_verification_code(activation_id, result)
                logger.info(
                    "SMSBower 已获取验证码: mobile=%s, poll=%d",
                    mask_phone(mobile_number.mobile_number),
                    poll_count,
                )
                return result.code

            remaining_seconds = deadline - self._monotonic_clock()
            if remaining_seconds <= 0:
                logger.warning(
                    "SMSBower 等待验证码超时: mobile=%s, timeout=%s",
                    mask_phone(mobile_number.mobile_number),
                    format_duration(self._config.verification_code_wait_timeout),
                )
                return None

            self._sleeper(min(self._poll_interval_seconds, remaining_seconds))

    def _query_latest_verification_code(
            self,
            activation_id: str,
    ) -> SmsBowerVerificationCodeResult | None:
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
            activation_id = _read_mobile_attribute(mobile_number, "activation_id")
            if activation_id is not None:
                try:
                    self._mark_verification_code_usable(activation_id)
                except Exception:
                    logger.exception(
                        "SMSBower 更新验证码可用时间失败，已忽略: activation_id=%s",
                        activation_id,
                    )
            return

        activation_id = _read_mobile_attribute(mobile_number, "activation_id")
        if activation_id is not None:
            self._mark_activation_unavailable(activation_id, "未收到验证码或手机号不可用")

        try:
            if activation_id is None:
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

    def _record_verification_code(
            self,
            activation_id: str,
            result: SmsBowerVerificationCodeResult,
    ) -> None:
        if self._activation_store is None:
            return
        self._activation_store.record_verification_code(
            provider=self.PROVIDER,
            activation_id=activation_id,
            entry=VerificationCodeEntry(
                code=result.code,
                text=result.text,
                received_at=self._now(),
                raw={"status_text": result.raw},
            ),
        )

    def _mark_activation_unavailable(self, activation_id: str, error: str) -> None:
        if self._activation_store is None:
            return
        self._activation_store.mark_unavailable(
            provider=self.PROVIDER,
            activation_id=activation_id,
            error=error,
            failed_at=self._now(),
        )

    def _mark_verification_code_usable(self, activation_id: str) -> None:
        if self._activation_store is None:
            return
        self._activation_store.mark_verification_code_usable(
            provider=self.PROVIDER,
            activation_id=activation_id,
            usable_at=self._now(),
        )

    def _request_json(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request_raw(params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmsServiceError(f"SMSBower 返回了非 JSON 响应: {response.text}") from exc

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
        activation_valid_seconds=_read_float(
            provider_config,
            "activation_valid_seconds",
            1500,
        ),
    )


def _create_mobile_number_from_record(
        record: SmsActivationRecord,
        *,
        reused_activation: bool,
) -> SmsMobileNumber:
    return SmsMobileNumber(
        mobile_number=record.mobile_number,
        attributes={
            "provider": record.provider,
            "activation_id": record.activation_id,
            "activation_cost": record.activation_cost,
            "country_code": record.country_code,
            "can_get_another_sms": record.can_get_another_sms,
            "activation_time": record.activation_time.isoformat(),
            "activation_end_time": record.activation_end_time.isoformat(),
            "activation_operator": record.activation_operator,
            "reused_activation": reused_activation,
            "raw": dict(record.raw),
        },
    )


def _extract_verification_code(status_text: str) -> SmsBowerVerificationCodeResult | None:
    normalized_status = status_text.strip()
    if normalized_status == "STATUS_WAIT_CODE":
        return None
    if normalized_status == "STATUS_CANCEL":
        raise SmsServiceError("SMSBower 激活已取消")
    if normalized_status.startswith("STATUS_OK:"):
        code = _read_status_code(normalized_status, "STATUS_OK:")
        if code is None:
            return None
        return SmsBowerVerificationCodeResult(code=code, raw=status_text)
    if normalized_status.startswith("STATUS_WAIT_RETRY:"):
        code = _read_status_code(normalized_status, "STATUS_WAIT_RETRY:")
        if code is None:
            return None
        return SmsBowerVerificationCodeResult(code=code, raw=status_text)

    raise SmsServiceError(f"SMSBower 返回了未知短信状态: {status_text}")


def _read_status_code(status_text: str, prefix: str) -> str | None:
    code = status_text.removeprefix(prefix).strip()
    code = code.strip("'\" ")
    return code or None


def _read_response_text(response: Any) -> str:
    return response.text.strip()


def _read_response_datetime(payload: dict[str, Any], key: str) -> datetime:
    value = payload.get(key)
    parsed_value = _parse_optional_datetime(value)
    if parsed_value is None:
        raise SmsServiceError(f"SMSBower 响应缺少有效时间字段: {key}")
    return parsed_value


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
        try:
            return datetime.strptime(stripped_value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
        except ValueError:
            return None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _read_mobile_attribute(
        mobile_number: SmsMobileNumber,
        key: str,
) -> str | None:
    value = mobile_number.get_attribute(key)
    if value is None or value == "":
        return None
    return str(value)


def _require_mobile_attribute(mobile_number: SmsMobileNumber, key: str) -> str:
    value = _read_mobile_attribute(mobile_number, key)
    if value is None:
        raise SmsServiceError(
            f"手机号 {mobile_number.mobile_number} 缺少 {key}，无法调用 SMSBower"
        )
    return value


def _is_enabled_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


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
