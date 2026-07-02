from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
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

# HeroSMS 实际返回的 activationTime 可能是无时区字符串，但语义是 UTC+3。
# 带时区的 ISO 字符串仍按原始时区正常转换。
HERO_SMS_NAIVE_DATETIME_TZ = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class HeroSmsServiceConfig:
    base_url: str
    api_key: str
    country_id: int
    max_price: float
    verification_code_wait_timeout: float = 125


@dataclass(frozen=True)
class HeroVerificationCodeResult:
    code: str
    text: str = ""
    received_at: datetime | None = None
    raw: Mapping[str, Any] | None = None


class HeroSmsService(SmsService):
    """
    对接 HeroSMS 手机号激活接口。
    """

    PROVIDER = "hero_sms"
    SERVICE_CODE = "dr"
    CANCEL_STATUS = 8
    REQUEST_NEW_SMS_STATUS = 3
    POLL_INTERVAL_SECONDS = 5
    CLEANUP_UNRECEIVED_MIN_AGE_SECONDS = 120

    def __init__(
            self,
            config: HeroSmsServiceConfig,
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
            raise ValueError("HeroSMS 轮询间隔必须大于 0")
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
            "HeroSMS 服务已初始化: country_id=%s, max_price=%s, code_timeout=%s, "
            "reuse_local=%s",
            config.country_id,
            config.max_price,
            format_duration(config.verification_code_wait_timeout),
            self._activation_store_config.reuse_local_activation,
        )
        self._cleanup_unreceived_activations()

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

        logger.info("HeroSMS 申请新手机号")
        return self._request_new_mobile_number()

    def _get_reusable_local_mobile_number(
            self,
            excluded_activation_ids: Collection[str] | None,
    ) -> SmsMobileNumber | None:
        if self._activation_store is None:
            return None

        records = self._list_reusable_local_activations(excluded_activation_ids)
        if (
                not records
                and self._activation_store_config.wait_reusable_activation_enabled
        ):
            waitable_record = (
                self._activation_store.find_next_waitable_reusable_activation(
                    provider=self.PROVIDER,
                    service_code=self.SERVICE_CODE,
                    excluded_activation_ids=excluded_activation_ids,
                    now=self._now(),
                    reuse_min_interval_seconds=(
                        self._activation_store_config.reuse_min_interval_seconds
                    ),
                    min_remaining_seconds=self._config.verification_code_wait_timeout,
                )
            )
            if waitable_record is not None:
                logger.info(
                    "HeroSMS 等待本地激活可复用: mobile=%s, activation_id=%s, "
                    "wait=%.3fs",
                    mask_phone(waitable_record.record.mobile_number),
                    waitable_record.record.activation_id,
                    waitable_record.wait_seconds,
                )
                self._sleeper(waitable_record.wait_seconds)
                records = self._list_reusable_local_activations(
                    excluded_activation_ids,
                )

        for record in records:
            try:
                logger.info(
                    "HeroSMS 尝试复用本地激活: mobile=%s, activation_id=%s, end_time=%s",
                    mask_phone(record.mobile_number),
                    record.activation_id,
                    record.activation_end_time.isoformat(),
                )
                self._request_new_sms_for_activation(record.activation_id)
            except Exception as exc:
                logger.exception(
                    "HeroSMS 本地激活请求新验证码失败，标记不可用: activation_id=%s",
                    record.activation_id,
                )
                self._mark_activation_unavailable(record.activation_id, str(exc))
                continue

            logger.info(
                "HeroSMS 本地激活复用成功: mobile=%s, activation_id=%s",
                mask_phone(record.mobile_number),
                record.activation_id,
            )
            return _create_mobile_number_from_record(record, reused_activation=True)

        return None

    def _list_reusable_local_activations(
            self,
            excluded_activation_ids: Collection[str] | None,
    ) -> list[SmsActivationRecord]:
        if self._activation_store is None:
            return []
        return self._activation_store.list_reusable_activations(
            provider=self.PROVIDER,
            service_code=self.SERVICE_CODE,
            excluded_activation_ids=excluded_activation_ids,
            now=self._now(),
            reuse_min_interval_seconds=(
                self._activation_store_config.reuse_min_interval_seconds
            ),
            min_remaining_seconds=self._config.verification_code_wait_timeout,
        )

    def _request_new_mobile_number(self) -> SmsMobileNumber:
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
        activation_time = _read_response_datetime(payload, "activationTime")
        activation_end_time = _read_response_datetime(payload, "activationEndTime")
        logger.info(
            "HeroSMS 新手机号申请成功: mobile=%s, activation_id=%s, cost=%s",
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
            currency=payload.get("currency"),
            country_code=payload.get("countryCode"),
            country_phone_code=payload.get("countryPhoneCode"),
            activation_operator=payload.get("activationOperator"),
            activation_time=activation_time,
            activation_end_time=activation_end_time,
            can_get_another_sms=_is_enabled_flag(payload.get("canGetAnotherSms")),
            raw=payload,
        )
        if self._activation_store is not None:
            self._activation_store.upsert_activation(record)
        return _create_mobile_number_from_record(record, reused_activation=False)

    def _cleanup_unreceived_activations(self) -> None:
        if self._activation_store is None:
            return

        try:
            records = self._activation_store.list_unreceived_activations_for_cleanup(
                provider=self.PROVIDER,
                now=self._now(),
                min_age_seconds=self.CLEANUP_UNRECEIVED_MIN_AGE_SECONDS,
            )
        except Exception:
            logger.exception("HeroSMS 查询本地待清理激活失败，已忽略")
            return

        for record in records:
            try:
                logger.info(
                    "HeroSMS 清理未收到验证码的激活: mobile=%s, activation_id=%s",
                    mask_phone(record.mobile_number),
                    record.activation_id,
                )
                self._request_raw(
                    {
                        "action": "setStatus",
                        "id": record.activation_id,
                        "status": self.CANCEL_STATUS,
                        "api_key": self._config.api_key,
                    }
                )
            except Exception:
                logger.exception(
                    "HeroSMS 清理未收验证码激活失败，已忽略: activation_id=%s",
                    record.activation_id,
                )
            finally:
                self._mark_activation_unavailable(
                    record.activation_id,
                    "初始化清理未收到验证码的 HeroSMS 激活",
                )

    def _request_new_sms_for_activation(self, activation_id: str) -> None:
        self._request_raw(
            {
                "action": "setStatus",
                "id": activation_id,
                "status": self.REQUEST_NEW_SMS_STATUS,
                "api_key": self._config.api_key,
            }
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
            result = self._query_latest_verification_code(activation_id, sent_after)
            if result is not None:
                self._record_verification_code(activation_id, result)
                logger.info(
                    "HeroSMS 已获取验证码: mobile=%s, poll=%d",
                    mask_phone(mobile_number.mobile_number),
                    poll_count,
                )
                return result.code

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
    ) -> HeroVerificationCodeResult | None:
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
            activation_id = _read_mobile_attribute(mobile_number, "activation_id")
            if activation_id is not None:
                try:
                    self._mark_verification_code_usable(activation_id)
                except Exception:
                    logger.exception(
                        "HeroSMS 更新验证码可用时间失败，已忽略: activation_id=%s",
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

    def _record_verification_code(
            self,
            activation_id: str,
            result: HeroVerificationCodeResult,
    ) -> None:
        if self._activation_store is None:
            return
        self._activation_store.record_verification_code(
            provider=self.PROVIDER,
            activation_id=activation_id,
            entry=VerificationCodeEntry(
                code=result.code,
                text=result.text,
                received_at=result.received_at or self._now(),
                raw=result.raw or {},
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
            raise SmsServiceError(f"HeroSMS 返回了非 JSON 响应: {response.text}") from exc

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
            "currency": record.currency,
            "country_code": record.country_code,
            "country_phone_code": record.country_phone_code,
            "can_get_another_sms": record.can_get_another_sms,
            "activation_time": record.activation_time.isoformat(),
            "activation_end_time": record.activation_end_time.isoformat(),
            "activation_operator": record.activation_operator,
            "reused_activation": reused_activation,
            "raw": dict(record.raw),
        },
    )


def _extract_latest_verification_code(
        payload: Mapping[str, Any],
        sent_after: datetime,
) -> HeroVerificationCodeResult | None:
    normalized_sent_after = _normalize_datetime(sent_after)
    oldest_datetime = datetime.min.replace(tzinfo=UTC)
    candidates: list[tuple[HeroVerificationCodeResult, datetime | None, int]] = []

    for priority, channel_name in ((1, "sms"), (0, "call")):
        channel_payload = payload.get(channel_name)
        if not isinstance(channel_payload, Mapping):
            continue

        code = _read_optional_code(channel_payload)
        if code is None:
            continue

        received_at = _parse_optional_datetime(channel_payload.get("dateTime"))
        if received_at is not None and received_at < normalized_sent_after:
            continue

        candidates.append(
            (
                HeroVerificationCodeResult(
                    code=code,
                    text=str(channel_payload.get("text") or ""),
                    received_at=received_at,
                    raw=channel_payload,
                ),
                received_at,
                priority,
            )
        )

    if not candidates:
        return None

    result, _, _ = max(
        candidates,
        key=lambda candidate: (
            candidate[1] is not None,
            candidate[1] or oldest_datetime,
            candidate[2],
        ),
    )
    return result


def _read_optional_code(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("code")
    if value is None:
        return None

    code = str(value).strip()
    if not code or code.lower() == "null":
        return None
    return code


def _read_response_datetime(payload: Mapping[str, Any], key: str) -> datetime:
    value = payload.get(key)
    parsed_value = _parse_optional_datetime(value)
    if parsed_value is None:
        raise SmsServiceError(f"HeroSMS 响应缺少有效时间字段: {key}")
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
        return _normalize_datetime(
            datetime.fromisoformat(iso_value),
            naive_timezone=HERO_SMS_NAIVE_DATETIME_TZ,
        )
    except ValueError:
        return None


def _normalize_datetime(
        value: datetime,
        *,
        naive_timezone=UTC,
) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=naive_timezone).astimezone(UTC)
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
            f"手机号 {mobile_number.mobile_number} 缺少 {key}，无法调用 HeroSMS"
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
