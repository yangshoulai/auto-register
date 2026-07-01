from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmsActivationRecord:
    provider: str
    service_code: str
    mobile_number: str
    activation_id: str
    activation_time: datetime
    activation_end_time: datetime
    can_get_another_sms: bool
    activation_cost: Any = None
    currency: Any = None
    country_code: Any = None
    country_phone_code: Any = None
    activation_operator: Any = None
    verification_code_received_count: int = 0
    last_verification_code_received_at: datetime | None = None
    verification_codes: tuple[Mapping[str, Any], ...] = ()
    is_available: bool = True
    last_error: str | None = None
    last_failed_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationCodeEntry:
    code: str
    text: str = ""
    received_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class SmsActivationStore:
    """
    本地短信激活 SQLite 存储。
    """

    def __init__(self, sqlite_path: str | Path) -> None:
        self._sqlite_path = Path(sqlite_path)
        self._ensure_database()

    @property
    def sqlite_path(self) -> Path:
        return self._sqlite_path

    def upsert_activation(self, record: SmsActivationRecord) -> None:
        now_text = _serialize_datetime(_utc_now())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sms_activations (
                    provider,
                    service_code,
                    mobile_number,
                    activation_id,
                    activation_cost,
                    currency,
                    country_code,
                    country_phone_code,
                    activation_operator,
                    activation_time,
                    activation_end_time,
                    can_get_another_sms,
                    raw_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, activation_id) DO UPDATE SET
                    service_code = excluded.service_code,
                    mobile_number = excluded.mobile_number,
                    activation_cost = excluded.activation_cost,
                    currency = excluded.currency,
                    country_code = excluded.country_code,
                    country_phone_code = excluded.country_phone_code,
                    activation_operator = excluded.activation_operator,
                    activation_time = excluded.activation_time,
                    activation_end_time = excluded.activation_end_time,
                    can_get_another_sms = excluded.can_get_another_sms,
                    is_available = 1,
                    last_error = NULL,
                    last_failed_at = NULL,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.provider,
                    record.service_code,
                    record.mobile_number,
                    record.activation_id,
                    _optional_text(record.activation_cost),
                    _optional_text(record.currency),
                    _optional_text(record.country_code),
                    _optional_text(record.country_phone_code),
                    _optional_text(record.activation_operator),
                    _serialize_datetime(record.activation_time),
                    _serialize_datetime(record.activation_end_time),
                    1 if record.can_get_another_sms else 0,
                    _json_dumps(record.raw),
                    now_text,
                    now_text,
                ),
            )
        logger.info(
            "短信激活记录已写入本地库: provider=%s, activation_id=%s, mobile=%s",
            record.provider,
            record.activation_id,
            record.mobile_number,
        )

    def list_reusable_activations(
            self,
            *,
            provider: str,
            service_code: str,
            excluded_activation_ids: Collection[str] | None,
            now: datetime,
            reuse_min_interval_seconds: float,
    ) -> list[SmsActivationRecord]:
        normalized_now = _normalize_datetime(now)
        min_received_at = normalized_now - timedelta(
            seconds=reuse_min_interval_seconds,
        )
        excluded_ids = {str(value) for value in excluded_activation_ids or ()}
        params: list[Any] = [
            provider,
            service_code,
            _serialize_datetime(normalized_now),
            _serialize_datetime(min_received_at),
        ]
        excluded_clause = ""
        if excluded_ids:
            placeholders = ",".join("?" for _ in excluded_ids)
            excluded_clause = f"AND activation_id NOT IN ({placeholders})"
            params.extend(sorted(excluded_ids))

        sql = f"""
            SELECT *
            FROM sms_activations
            WHERE provider = ?
              AND service_code = ?
              AND is_available = 1
              AND can_get_another_sms = 1
              AND activation_end_time > ?
              AND (
                    last_verification_code_received_at IS NULL
                    OR last_verification_code_received_at <= ?
                  )
              {excluded_clause}
            ORDER BY activation_end_time ASC
        """
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        records = [_row_to_record(row) for row in rows]
        logger.info(
            "本地可复用短信激活查询完成: provider=%s, count=%d, excluded_count=%d",
            provider,
            len(records),
            len(excluded_ids),
        )
        return records

    def list_unreceived_activations_for_cleanup(
            self,
            *,
            provider: str,
            now: datetime,
            min_age_seconds: float,
    ) -> list[SmsActivationRecord]:
        normalized_now = _normalize_datetime(now)
        max_activation_time = normalized_now - timedelta(seconds=min_age_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM sms_activations
                WHERE provider = ?
                  AND is_available = 1
                  AND verification_code_received_count = 0
                  AND activation_time <= ?
                ORDER BY activation_time ASC
                """,
                (
                    provider,
                    _serialize_datetime(max_activation_time),
                ),
            ).fetchall()

        records = [_row_to_record(row) for row in rows]
        logger.info(
            "本地待清理未收验证码激活查询完成: provider=%s, count=%d, min_age=%s",
            provider,
            len(records),
            min_age_seconds,
        )
        return records

    def record_verification_code(
            self,
            *,
            provider: str,
            activation_id: str,
            entry: VerificationCodeEntry,
    ) -> None:
        received_at = _normalize_datetime(entry.received_at or _utc_now())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT verification_codes_json
                FROM sms_activations
                WHERE provider = ?
                  AND activation_id = ?
                """,
                (provider, activation_id),
            ).fetchone()
            if row is None:
                logger.warning(
                    "短信激活记录不存在，跳过验证码记录更新: provider=%s, activation_id=%s",
                    provider,
                    activation_id,
                )
                return

            verification_codes = _json_loads_list(row["verification_codes_json"])
            verification_codes.append(
                {
                    "code": entry.code,
                    "text": entry.text,
                    "received_at": _serialize_datetime(received_at),
                    "raw": dict(entry.raw),
                }
            )
            connection.execute(
                """
                UPDATE sms_activations
                SET verification_code_received_count   =
                        verification_code_received_count + 1,
                    last_verification_code_received_at = ?,
                    verification_codes_json            = ?,
                    updated_at                         = ?
                WHERE provider = ?
                  AND activation_id = ?
                """,
                (
                    _serialize_datetime(received_at),
                    _json_dumps(verification_codes),
                    _serialize_datetime(_utc_now()),
                    provider,
                    activation_id,
                ),
            )
        logger.info(
            "短信激活验证码记录已更新: provider=%s, activation_id=%s",
            provider,
            activation_id,
        )

    def mark_unavailable(
            self,
            *,
            provider: str,
            activation_id: str,
            error: str,
            failed_at: datetime | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sms_activations
                SET is_available   = 0,
                    last_error     = ?,
                    last_failed_at = ?,
                    updated_at     = ?
                WHERE provider = ?
                  AND activation_id = ?
                """,
                (
                    error,
                    _serialize_datetime(failed_at or _utc_now()),
                    _serialize_datetime(_utc_now()),
                    provider,
                    activation_id,
                ),
            )
        logger.info(
            "短信激活已标记为不可用: provider=%s, activation_id=%s, error=%s",
            provider,
            activation_id,
            error,
        )

    def _ensure_database(self) -> None:
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sms_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    service_code TEXT NOT NULL,
                    mobile_number TEXT NOT NULL,
                    activation_id TEXT NOT NULL,
                    activation_cost TEXT,
                    currency TEXT,
                    country_code TEXT,
                    country_phone_code TEXT,
                    activation_operator TEXT,
                    activation_time TEXT NOT NULL,
                    activation_end_time TEXT NOT NULL,
                    can_get_another_sms INTEGER NOT NULL DEFAULT 0,
                    verification_code_received_count INTEGER NOT NULL DEFAULT 0,
                    last_verification_code_received_at TEXT,
                    verification_codes_json TEXT NOT NULL DEFAULT '[]',
                    is_available INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT,
                    last_failed_at TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, activation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sms_activations_reusable
                ON sms_activations (
                    provider,
                    service_code,
                    is_available,
                    can_get_another_sms,
                    activation_end_time
                    )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._sqlite_path)
        connection.row_factory = sqlite3.Row
        return connection


def _row_to_record(row: sqlite3.Row) -> SmsActivationRecord:
    return SmsActivationRecord(
        provider=str(row["provider"]),
        service_code=str(row["service_code"]),
        mobile_number=str(row["mobile_number"]),
        activation_id=str(row["activation_id"]),
        activation_cost=row["activation_cost"],
        currency=row["currency"],
        country_code=row["country_code"],
        country_phone_code=row["country_phone_code"],
        activation_operator=row["activation_operator"],
        activation_time=_parse_datetime(row["activation_time"]),
        activation_end_time=_parse_datetime(row["activation_end_time"]),
        can_get_another_sms=bool(row["can_get_another_sms"]),
        verification_code_received_count=int(row["verification_code_received_count"]),
        last_verification_code_received_at=_parse_optional_datetime(
            row["last_verification_code_received_at"]
        ),
        verification_codes=tuple(_json_loads_list(row["verification_codes_json"])),
        is_available=bool(row["is_available"]),
        last_error=row["last_error"],
        last_failed_at=_parse_optional_datetime(row["last_failed_at"]),
        raw=_json_loads_dict(row["raw_json"]),
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _json_loads_list(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return _parse_datetime(value)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if not isinstance(value, str):
        raise TypeError(f"时间字段必须是字符串或 datetime: {value!r}")
    return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _serialize_datetime(value: datetime) -> str:
    return _normalize_datetime(value).isoformat()


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
