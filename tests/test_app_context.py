from __future__ import annotations

import unittest
from urllib.parse import urlparse

from core.app_context import create_app_context
from core.config import (
    AccountExportServiceConfig,
    AccountServiceConfig,
    AppConfig,
    EmailServiceConfig,
)
from core.http_service import HttpService


class FakeRawSession:
    def __init__(self) -> None:
        self.closed = False
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        parsed_url = urlparse(url)
        path = parsed_url.path
        if parsed_url.query:
            path = f"{path}?{parsed_url.query}"

        self.requests.append(
            {
                "method": method,
                "path": path,
                "timeout": kwargs.get("timeout"),
            }
        )
        if path == "/api/extension/login":
            return FakeResponse(
                {
                    "success": True,
                    "launch_url": "/extension-login/token?next=/%23settings",
                    "expires_in": 60,
                }
            )
        if path == "/extension-login/token?next=/%23settings":
            return FakeResponse({})
        if path == "/api/csrf-token":
            return FakeResponse(
                {
                    "success": True,
                    "csrf_token": "csrf-token",
                    "csrf_disabled": False,
                }
            )
        raise AssertionError(f"未预期的请求: {method} {path}")

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeSessionFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeRawSession] = []

    def create(self) -> FakeRawSession:
        session = FakeRawSession()
        self.sessions.append(session)
        return session


class AppContextTest(unittest.TestCase):
    def test_create_app_context_initializes_services_and_closes_http_sessions(self) -> None:
        session_factory = FakeSessionFactory()
        http_service = HttpService(
            default_timeout=7,
            session_factory=session_factory.create,
        )
        config = AppConfig(
            account_service=AccountServiceConfig(specified_password="Passw0rd!"),
            account_export_service=AccountExportServiceConfig(
                provider="cpa",
                provider_config={
                    "base_url": "https://cpa.example.test/v0/management",
                    "secret_key": "management-secret",
                },
            ),
            email_service=EmailServiceConfig(
                provider="outlook_mail",
                provider_config={
                    "base_url": "https://mail.example.test",
                    "admin_password": "web-login-password",
                    "use_temp_email": True,
                    "temp_email": {
                        "provider": "cloudflare",
                        "channel_id": "1",
                        "domain": "edu.lamanujin.store",
                    },
                },
            ),
        )

        context = create_app_context(config, http_service=http_service)

        self.assertEqual(context.config, config)
        self.assertEqual(context.account_service.create_account().password, "Passw0rd!")
        self.assertIs(context.http_service, http_service)
        self.assertIsNone(context.sms_service)
        self.assertEqual(len(session_factory.sessions), 1)
        self.assertEqual(session_factory.sessions[0].requests[0]["timeout"], 7)

        context.close()

        self.assertTrue(session_factory.sessions[0].closed)


if __name__ == "__main__":
    unittest.main()
