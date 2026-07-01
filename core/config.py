from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass(frozen=True)
class AccountServiceConfig:
    specified_password: str | None = None


@dataclass(frozen=True)
class AccountExportServiceConfig:
    provider: str = "cpa"
    provider_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailServiceConfig:
    provider: str = "outlook_mail"
    provider_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SmsActivationStoreConfig:
    sqlite_path: str = "data/sms_activations.db"
    reuse_local_activation: bool = True
    reuse_min_interval_seconds: float = 900


@dataclass(frozen=True)
class SmsServiceConfig:
    provider: str | None = None
    provider_config: dict[str, Any] = field(default_factory=dict)
    activation_store: SmsActivationStoreConfig = field(
        default_factory=SmsActivationStoreConfig
    )


@dataclass(frozen=True)
class HttpServiceConfig:
    default_timeout: float = 30
    user_agent: str | None = None
    proxy_url: str | None = None
    default_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    use_colors: bool = True


@dataclass(frozen=True)
class RegisterConfig:
    verification_code_wait_timeout: float = 60
    phone_number_retry_attempts: int = 1
    sms_verification_retry_attempts: int = 5


@dataclass(frozen=True)
class AppConfig:
    account_service: AccountServiceConfig = field(default_factory=AccountServiceConfig)
    account_export_service: AccountExportServiceConfig = field(
        default_factory=AccountExportServiceConfig
    )
    http_service: HttpServiceConfig = field(default_factory=HttpServiceConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    email_service: EmailServiceConfig = field(default_factory=EmailServiceConfig)
    sms_service: SmsServiceConfig = field(default_factory=SmsServiceConfig)
    register: RegisterConfig = field(default_factory=RegisterConfig)


def load_config(path: str | Path = CONFIG_PATH) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()

    with config_path.open("rb") as config_file:
        raw_config = tomllib.load(config_file)

    account_service_config = _read_table(raw_config, "account_service")
    account_export_service_config = _read_table(raw_config, "account_export_service")
    http_service_config = _read_table(raw_config, "http_service")
    logging_config = _read_table(raw_config, "logging")
    email_service_config = _read_table(raw_config, "email_service")
    sms_service_config = _read_table(raw_config, "sms_service")
    register_config = _read_table(raw_config, "register")

    specified_password = _read_optional_string(
        account_service_config,
        "specified_password",
    )

    account_export_provider = (
        _read_optional_string(account_export_service_config, "provider") or "cpa"
    )
    account_export_providers_config = _read_table(
        account_export_service_config,
        "providers",
    )
    account_export_provider_config = account_export_providers_config.get(
        account_export_provider,
        {},
    )
    if not isinstance(account_export_provider_config, dict):
        raise TypeError(
            "config.toml 中的 "
            f"[account_export_service.providers.{account_export_provider}] "
            "必须是 TOML 表"
        )

    http_default_headers = _read_string_table(http_service_config, "default_headers")
    http_user_agent = _read_optional_string(http_service_config, "user_agent")
    if http_user_agent is not None:
        http_default_headers["User-Agent"] = http_user_agent

    email_provider = (
        _read_optional_string(email_service_config, "provider") or "outlook_mail"
    )
    email_providers_config = _read_table(email_service_config, "providers")
    email_provider_config = email_providers_config.get(email_provider, {})
    if not isinstance(email_provider_config, dict):
        raise TypeError(
            f"config.toml 中的 [email_service.providers.{email_provider}] 必须是 TOML 表"
        )

    sms_provider = _read_optional_string(sms_service_config, "provider")
    sms_activation_store_config = _read_table(
        sms_service_config,
        "activation_store",
    )
    sms_provider_config: dict[str, Any] = {}
    if sms_provider is not None:
        sms_providers_config = _read_table(sms_service_config, "providers")
        raw_sms_provider_config = sms_providers_config.get(sms_provider, {})
        if not isinstance(raw_sms_provider_config, dict):
            raise TypeError(
                f"config.toml 中的 [sms_service.providers.{sms_provider}] 必须是 TOML 表"
            )
        sms_provider_config = dict(raw_sms_provider_config)

    return AppConfig(
        account_service=AccountServiceConfig(
            specified_password=specified_password,
        ),
        account_export_service=AccountExportServiceConfig(
            provider=account_export_provider,
            provider_config=dict(account_export_provider_config),
        ),
        http_service=HttpServiceConfig(
            default_timeout=_read_float(http_service_config, "default_timeout", 30),
            user_agent=http_user_agent,
            proxy_url=_read_optional_string(http_service_config, "proxy_url"),
            default_headers=http_default_headers,
        ),
        logging=LoggingConfig(
            level=_read_optional_string(logging_config, "level") or "INFO",
            use_colors=_read_bool(logging_config, "use_colors", True),
        ),
        email_service=EmailServiceConfig(
            provider=email_provider,
            provider_config=dict(email_provider_config),
        ),
        sms_service=SmsServiceConfig(
            provider=sms_provider,
            provider_config=sms_provider_config,
            activation_store=SmsActivationStoreConfig(
                sqlite_path=_read_optional_string(
                    sms_activation_store_config,
                    "sqlite_path",
                )
                or "data/sms_activations.db",
                reuse_local_activation=_read_bool(
                    sms_activation_store_config,
                    "reuse_local_activation",
                    True,
                ),
                reuse_min_interval_seconds=_read_float(
                    sms_activation_store_config,
                    "reuse_min_interval_seconds",
                    900,
                ),
            ),
        ),
        register=RegisterConfig(
            verification_code_wait_timeout=_read_float(
                register_config,
                "verification_code_wait_timeout",
                60,
            ),
            phone_number_retry_attempts=_read_non_negative_int(
                register_config,
                "phone_number_retry_attempts",
                1,
            ),
            sms_verification_retry_attempts=_read_non_negative_int(
                register_config,
                "sms_verification_retry_attempts",
                5,
            ),
        ),
    )


def _read_table(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"config.toml 中的 [{key}] 必须是 TOML 表")
    return value


def _read_optional_string(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TypeError(f"配置项 {key} 必须是字符串")
    return value


def _read_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"配置项 {key} 必须是数字")
    if value <= 0:
        raise ValueError(f"配置项 {key} 必须大于 0")
    return float(value)


def _read_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    raise TypeError(f"配置项 {key} 必须是布尔值")


def _read_non_negative_int(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"配置项 {key} 必须是整数")
    if value < 0:
        raise ValueError(f"配置项 {key} 必须大于等于 0")
    return value


def _read_string_table(config: dict[str, Any], key: str) -> dict[str, str]:
    value = config.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"config.toml 中的 [{key}] 必须是 TOML 表")

    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, str):
            raise TypeError(f"配置项 {key} 必须是字符串到字符串的映射")
        if item_key and item_value:
            result[item_key] = item_value
    return result


settings = load_config()
