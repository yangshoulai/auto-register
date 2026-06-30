from __future__ import annotations

import unittest

from core.logging_config import sanitize_mapping, sanitize_url


class LoggingConfigTest(unittest.TestCase):
    def test_sanitize_url_keeps_sensitive_query_values(self) -> None:
        sanitized_url = sanitize_url(
            "http://localhost:1455/auth/callback?code=oauth-code&state=state-token"
        )

        self.assertEqual(
            sanitized_url,
            "http://localhost:1455/auth/callback?code=oauth-code&state=state-token",
        )

    def test_sanitize_mapping_keeps_sensitive_values(self) -> None:
        sanitized_mapping = sanitize_mapping(
            {
                "api_key": "secret-api-key",
                "password": "secret-password",
                "redirect_url": "http://localhost:1455/auth/callback?token=secret",
                "normal": "value",
            }
        )

        self.assertEqual(
            sanitized_mapping,
            {
                "api_key": "secret-api-key",
                "password": "secret-password",
                "redirect_url": "http://localhost:1455/auth/callback?token=secret",
                "normal": "value",
            },
        )


if __name__ == "__main__":
    unittest.main()
