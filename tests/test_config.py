from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import load_config


class ConfigTest(unittest.TestCase):
    def test_load_config_reads_register_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """
[register]
verification_code_wait_timeout = 75
phone_number_retry_attempts = 3
sms_verification_retry_attempts = 7
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.register.verification_code_wait_timeout, 75)
        self.assertEqual(config.register.phone_number_retry_attempts, 3)
        self.assertEqual(config.register.sms_verification_retry_attempts, 7)

    def test_load_config_uses_default_register_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "missing.toml"

            config = load_config(config_path)

        self.assertEqual(config.register.verification_code_wait_timeout, 60)
        self.assertEqual(config.register.phone_number_retry_attempts, 1)
        self.assertEqual(config.register.sms_verification_retry_attempts, 5)

    def test_load_config_reads_http_service_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """
[http_service]
default_timeout = 12.5
user_agent = "AutoRegister/1.0"
proxy_url = "http://127.0.0.1:7890"

[http_service.default_headers]
X-Trace = "enabled"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.http_service.default_timeout, 12.5)
        self.assertEqual(config.http_service.user_agent, "AutoRegister/1.0")
        self.assertEqual(config.http_service.proxy_url, "http://127.0.0.1:7890")
        self.assertEqual(
            config.http_service.default_headers,
            {
                "X-Trace": "enabled",
                "User-Agent": "AutoRegister/1.0",
            },
        )

    def test_load_config_reads_logging_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """
[logging]
level = "DEBUG"
use_colors = false
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.logging.level, "DEBUG")
        self.assertFalse(config.logging.use_colors)

    def test_load_config_reads_sms_service_provider_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """
[sms_service]
provider = "hero_sms"

[sms_service.activation_store]
sqlite_path = "runtime/sms.db"
reuse_local_activation = false
reuse_min_interval_seconds = 600

[sms_service.providers.hero_sms]
base_url = "https://hero-sms.com/stubs/handler_api.php"
api_key = "api-key"
country_id = 6
max_price = 12.5
verification_code_wait_timeout = 125
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.sms_service.provider, "hero_sms")
        self.assertEqual(
            config.sms_service.provider_config,
            {
                "base_url": "https://hero-sms.com/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": 6,
                "max_price": 12.5,
                "verification_code_wait_timeout": 125,
            },
        )
        self.assertEqual(config.sms_service.activation_store.sqlite_path, "runtime/sms.db")
        self.assertFalse(config.sms_service.activation_store.reuse_local_activation)
        self.assertEqual(
            config.sms_service.activation_store.reuse_min_interval_seconds,
            600,
        )

    def test_load_config_reads_account_export_service_provider_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """
[account_export_service]
provider = "cpa"

[account_export_service.providers.cpa]
base_url = "http://localhost:8317/v0/management"
secret_key = "management-secret"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.account_export_service.provider, "cpa")
        self.assertEqual(
            config.account_export_service.provider_config,
            {
                "base_url": "http://localhost:8317/v0/management",
                "secret_key": "management-secret",
            },
        )


if __name__ == "__main__":
    unittest.main()
