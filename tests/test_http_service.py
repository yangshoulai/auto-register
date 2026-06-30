from __future__ import annotations

import unittest

from core.http_service import HttpService


class FakeSession:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "timeout": kwargs.get("timeout"),
                "headers": kwargs.get("headers"),
                "proxy": kwargs.get("proxy"),
            }
        )
        return object()


class HttpServiceTest(unittest.TestCase):
    def test_request_applies_default_timeout_headers_and_proxy(self) -> None:
        session = FakeSession()
        service = HttpService(
            default_timeout=9,
            default_headers={
                "User-Agent": "AutoRegister/1.0",
                "X-Trace": "default",
            },
            proxy_url="http://127.0.0.1:7890",
            session_factory=lambda: session,
        )

        service.request(
            "GET",
            "https://example.test",
            headers={"X-Trace": "override"},
        )

        self.assertEqual(
            session.requests[0],
            {
                "method": "GET",
                "url": "https://example.test",
                "timeout": 9,
                "headers": {
                    "User-Agent": "AutoRegister/1.0",
                    "X-Trace": "override",
                },
                "proxy": "http://127.0.0.1:7890",
            },
        )


if __name__ == "__main__":
    unittest.main()
