from __future__ import annotations

import unittest

from account_export.account_export_service import (
    AccountExportServiceError,
    create_account_export_service,
)
from account_export.cpa_account_export_service import (
    CpaAccountExportService,
    CpaAccountExportServiceConfig,
    create_cpa_account_export_service_config,
)
from core.config import AccountExportServiceConfig
from core.http_service import HttpService


class FakeResponse:
    def __init__(self, payload: dict | list | str, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else str(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers"),
            }
        )

        if not self._responses:
            raise AssertionError(f"未预期的请求: {method} {url}")
        return self._responses.pop(0)


def build_http_service(session: FakeSession) -> HttpService:
    return HttpService(session_factory=lambda: session)


def build_service(session: FakeSession) -> CpaAccountExportService:
    return CpaAccountExportService(
        CpaAccountExportServiceConfig(
            base_url="https://cpa.example.test/v0/management",
            secret_key="management-secret",
        ),
        http_service=build_http_service(session),
    )


class AccountExportServiceTest(unittest.TestCase):
    def test_get_oauth_url_returns_url_model_and_sends_management_key(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "status": "ok",
                        "state": "state-token",
                        "url": "https://auth.openai.com/oauth/authorize?state=state-token",
                    }
                )
            ]
        )
        service = build_service(session)

        oauth_url = service.get_oauth_url()

        self.assertEqual(
            oauth_url.url,
            "https://auth.openai.com/oauth/authorize?state=state-token",
        )
        self.assertEqual(oauth_url.state, "state-token")
        self.assertEqual(oauth_url.attributes["provider"], "cpa")
        self.assertEqual(
            session.requests[0],
            {
                "method": "GET",
                "url": "https://cpa.example.test/v0/management/codex-auth-url",
                "params": {"is_webui": "true"},
                "json": None,
                "headers": {"X-Management-Key": "management-secret"},
            },
        )

    def test_submit_redirect_url_posts_codex_redirect_url(self) -> None:
        session = FakeSession([FakeResponse({"status": "ok", "id": "account-id"})])
        service = build_service(session)

        result = service.submit_redirect_url("http://localhost:1455/auth/callback?code=1")

        self.assertTrue(result.success)
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.error)
        self.assertEqual(result.attributes["provider"], "cpa")
        self.assertEqual(
            session.requests[0],
            {
                "method": "POST",
                "url": "https://cpa.example.test/v0/management/oauth-callback",
                "params": None,
                "json": {
                    "provider": "codex",
                    "redirect_url": "http://localhost:1455/auth/callback?code=1",
                },
                "headers": {"X-Management-Key": "management-secret"},
            },
        )

    def test_submit_redirect_url_returns_error_result_for_cpa_error_status(self) -> None:
        session = FakeSession(
            [FakeResponse({"status": "error", "error": "state is required"})]
        )
        service = build_service(session)

        result = service.submit_redirect_url("http://localhost:1455/auth/callback")

        self.assertFalse(result.success)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "state is required")

    def test_get_oauth_url_raises_when_cpa_status_is_not_ok(self) -> None:
        session = FakeSession([FakeResponse({"status": "error", "error": "denied"})])
        service = build_service(session)

        with self.assertRaises(AccountExportServiceError):
            service.get_oauth_url()

    def test_http_error_raises_service_error(self) -> None:
        session = FakeSession([FakeResponse("server error", status_code=500)])
        service = build_service(session)

        with self.assertRaises(AccountExportServiceError):
            service.get_oauth_url()

    def test_non_json_response_raises_service_error(self) -> None:
        session = FakeSession([FakeResponse("not-json")])
        service = build_service(session)

        with self.assertRaises(AccountExportServiceError):
            service.get_oauth_url()

    def test_create_account_export_service_builds_cpa_provider(self) -> None:
        service = create_account_export_service(
            AccountExportServiceConfig(
                provider="cpa",
                provider_config={"secret_key": "management-secret"},
            ),
            http_service=build_http_service(FakeSession([])),
        )

        self.assertIsInstance(service, CpaAccountExportService)

    def test_cpa_config_uses_default_base_url_when_omitted(self) -> None:
        config = create_cpa_account_export_service_config(
            {"secret_key": "management-secret"}
        )

        self.assertEqual(
            config.base_url,
            CpaAccountExportService.DEFAULT_BASE_URL,
        )
        self.assertEqual(config.secret_key, "management-secret")


if __name__ == "__main__":
    unittest.main()
