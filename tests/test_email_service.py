from __future__ import annotations

import unittest
from datetime import UTC, datetime
from urllib.parse import urlparse

from core.config import EmailServiceConfig
from core.http_service import HttpService
from email.email_service import (
    EmailAccount,
    create_email_service,
)
from email.outlook_mail_email_service import (
    OutlookMailEmailService,
    OutlookMailEmailServiceConfig,
    OutlookMailOutlookConfig,
    OutlookMailTempEmailConfig,
)


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[tuple[str, str, dict | None]]) -> None:
        self._responses = responses
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        parsed_url = urlparse(url)
        path = parsed_url.path
        if parsed_url.query:
            path = f"{path}?{parsed_url.query}"

        self.requests.append(
            {
                "method": method,
                "path": path,
                "headers": kwargs.get("headers", {}),
                "json": kwargs.get("json"),
                "params": kwargs.get("params"),
            }
        )

        if not self._responses:
            raise AssertionError(f"未预期的请求: {method} {path}")

        expected_method, expected_path, payload = self._responses.pop(0)
        if (method, path) != (expected_method, expected_path):
            raise AssertionError(
                f"请求不匹配，期望 {expected_method} {expected_path}，实际 {method} {path}"
            )
        return FakeResponse(payload)


def build_http_service(session: FakeSession) -> HttpService:
    return HttpService(session_factory=lambda: session)


def build_temp_config() -> OutlookMailEmailServiceConfig:
    return OutlookMailEmailServiceConfig(
        base_url="https://mail.example.test",
        admin_password="web-login-password",
        use_temp_email=True,
        temp_email=OutlookMailTempEmailConfig(
            provider="cloudflare",
            channel_id="1",
            domain="edu.lamanujin.store",
        ),
    )


def build_outlook_config() -> OutlookMailEmailServiceConfig:
    return OutlookMailEmailServiceConfig(
        base_url="https://mail.example.test",
        admin_password="web-login-password",
        use_temp_email=False,
        outlook=OutlookMailOutlookConfig(
            pool_group_id=3,
            registered_group_id=4,
        ),
    )


def login_responses() -> list[tuple[str, str, dict | None]]:
    return [
        (
            "POST",
            "/api/extension/login",
            {
                "success": True,
                "launch_url": "/extension-login/token?next=/%23settings",
                "expires_in": 60,
            },
        ),
        ("GET", "/extension-login/token?next=/%23settings", None),
        (
            "GET",
            "/api/csrf-token",
            {"success": True, "csrf_token": "csrf-token", "csrf_disabled": False},
        ),
    ]


class OutlookMailEmailServiceTest(unittest.TestCase):
    def test_generate_temp_email_uses_csrf_and_returns_account_model(self) -> None:
        session = FakeSession(
            login_responses()
            + [
                (
                    "POST",
                    "/api/temp-emails/generate",
                    {
                        "success": True,
                        "email": "1a513f99@edu.lamanujin.store",
                        "message": "Cloudflare 临时邮箱创建成功",
                    },
                )
            ]
        )

        service = OutlookMailEmailService(
            build_temp_config(),
            http_service=build_http_service(session),
        )
        account = service.generate_email_address()

        self.assertEqual(account.email_address, "1a513f99@edu.lamanujin.store")
        self.assertEqual(account.get_attribute("mode"), "temp")
        self.assertEqual(
            session.requests[-1]["headers"],
            {"X-CSRFToken": "csrf-token"},
        )
        self.assertEqual(
            session.requests[-1]["json"],
            {
                "provider": "cloudflare",
                "channel_id": "1",
                "domain": "edu.lamanujin.store",
            },
        )

    def test_search_temp_email_fetches_detail_for_first_matching_message(self) -> None:
        session = FakeSession(
            login_responses()
            + [
                (
                    "GET",
                    "/api/temp-emails/bf3553635%40edu.lamanujin.store/messages",
                    {
                        "success": True,
                        "count": 2,
                        "emails": [
                            {
                                "id": "755",
                                "from": "security@openai.com",
                                "subject": "Ignore",
                                "timestamp": 1782465837,
                            },
                            {
                                "id": "756",
                                "from": "noreply@openai.com",
                                "subject": "ChatGPT 验证码",
                                "timestamp": 1782465838,
                            },
                        ],
                    },
                ),
                (
                    "GET",
                    "/api/temp-emails/bf3553635%40edu.lamanujin.store/messages/756",
                    {
                        "success": True,
                        "email": {
                            "id": "756",
                            "from": "noreply@openai.com",
                            "subject": "ChatGPT 验证码",
                            "timestamp": 1782465838,
                            "body": "<html>full body</html>",
                            "body_type": "html",
                        },
                    },
                ),
            ]
        )
        service = OutlookMailEmailService(
            build_temp_config(),
            http_service=build_http_service(session),
        )
        account = EmailAccount(
            email_address="bf3553635@edu.lamanujin.store",
            attributes={"mode": "temp"},
        )

        message = service.search_first_email(
            account,
            sent_after=datetime.fromtimestamp(1782465830, tz=UTC),
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.message_id, "756")
        self.assertEqual(message.body, "<html>full body</html>")
        self.assertEqual(message.body_type, "html")

    def test_outlook_callback_moves_used_account_to_registered_group(self) -> None:
        session = FakeSession(
            login_responses()
            + [
                (
                    "GET",
                    "/api/accounts",
                    {
                        "success": True,
                        "accounts": [
                            {
                                "id": 235,
                                "email": "kmdy0536@outlook.com",
                                "client_id": "client-id",
                                "refresh_token": "refresh-token",
                            }
                        ],
                    },
                ),
                ("PUT", "/api/accounts/235", {"success": True}),
            ]
        )

        service = OutlookMailEmailService(
            build_outlook_config(),
            http_service=build_http_service(session),
        )
        account = service.generate_email_address()
        service.callback(account, is_email_used=True)

        self.assertEqual(session.requests[-2]["params"], {"group_id": 3})
        self.assertEqual(
            session.requests[-1]["json"],
            {
                "email": "kmdy0536@outlook.com",
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "group_id": 4,
            },
        )

    def test_search_outlook_email_extracts_verification_code_from_html_body(self) -> None:
        session = FakeSession(
            login_responses()
            + [
                (
                    "GET",
                    "/api/accounts",
                    {
                        "success": True,
                        "accounts": [
                            {
                                "id": 235,
                                "email": "kmdy0536@outlook.com",
                                "client_id": "client-id",
                                "refresh_token": "refresh-token",
                            }
                        ],
                    },
                ),
                (
                    "GET",
                    "/api/emails/kmdy0536%40outlook.com",
                    {
                        "success": True,
                        "emails": [
                            {
                                "id": "msg-1",
                                "from": "verify@openai.com",
                                "subject": "Your OpenAI code",
                                "date": "2026-06-23T08:52:51Z",
                                "body_preview": "验证码",
                            }
                        ],
                    },
                ),
                (
                    "GET",
                    "/api/email/kmdy0536%40outlook.com/msg-1",
                    {
                        "success": True,
                        "email": {
                            "id": "msg-1",
                            "from": "verify@openai.com",
                            "subject": "Your OpenAI code",
                            "body": (
                                "<html><head><style>.code{color:red}</style>"
                                "<script>var ignored = 111111;</script></head>"
                                "<body><p>订单编号 1234567</p>"
                                "<div>验证码：<strong>654321</strong></div></body></html>"
                            ),
                            "body_type": "html",
                        },
                    },
                ),
            ]
        )
        service = OutlookMailEmailService(
            build_outlook_config(),
            http_service=build_http_service(session),
        )
        account = service.generate_email_address()

        message = service.search_first_email(
            account,
            sent_after=datetime(2026, 6, 23, tzinfo=UTC),
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.verification_code, "654321")

    def test_memory_email_service_is_not_supported(self) -> None:
        with self.assertRaises(ValueError):
            create_email_service(
                EmailServiceConfig(provider="memory", provider_config={})
            )


if __name__ == "__main__":
    unittest.main()
