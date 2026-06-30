from __future__ import annotations

import unittest
from datetime import UTC, datetime

from core.config import SmsServiceConfig
from core.http_service import HttpService
from sms.sms_bower_service import (
    SmsBowerService,
    SmsBowerServiceConfig,
    create_sms_bower_service_config,
)
from sms.sms_service import SmsMobileNumber, create_sms_service


class FakeResponse:
    def __init__(self, payload: dict | str | None = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else str(payload or {})

    def json(self) -> dict:
        if not isinstance(self._payload, dict):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses: list[dict | str | None]) -> None:
        self._responses = responses
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
            }
        )

        if not self._responses:
            raise AssertionError(f"未预期的请求: {method} {url}")

        return FakeResponse(self._responses.pop(0))


def build_http_service(session: FakeSession) -> HttpService:
    return HttpService(session_factory=lambda: session)


def build_config() -> SmsBowerServiceConfig:
    return SmsBowerServiceConfig(
        base_url="https://smsbower.page/stubs/handler_api.php",
        api_key="api-key",
        country_id=31,
        max_price=0.027,
        min_price=0.01,
    )


class FakePollClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


class SmsBowerServiceTest(unittest.TestCase):
    def test_get_mobile_number_returns_number_model_with_activation_id(self) -> None:
        session = FakeSession(
            [
                {
                    "activationId": 419892352,
                    "phoneNumber": "27657282036",
                    "activationCost": "0.027",
                    "countryCode": "31",
                    "canGetAnotherSms": "1",
                    "activationTime": "2026-06-29 05:48:49",
                    "activationOperator": None,
                }
            ]
        )
        service = SmsBowerService(build_config(), http_service=build_http_service(session))

        mobile_number = service.get_mobile_number()

        self.assertEqual(mobile_number.mobile_number, "27657282036")
        self.assertEqual(mobile_number.get_attribute("provider"), "sms_bower")
        self.assertEqual(mobile_number.get_attribute("activation_id"), "419892352")
        self.assertEqual(
            session.requests[0],
            {
                "method": "GET",
                "url": "https://smsbower.page/stubs/handler_api.php",
                "params": {
                    "api_key": "api-key",
                    "action": "getNumberV2",
                    "service": SmsBowerService.SERVICE_CODE,
                    "country": 31,
                    "maxPrice": 0.027,
                    "minPrice": 0.01,
                },
            },
        )

    def test_get_latest_verification_code_returns_none_when_waiting(self) -> None:
        session = FakeSession(["STATUS_WAIT_CODE"])
        config = SmsBowerServiceConfig(
            base_url="https://smsbower.page/stubs/handler_api.php",
            api_key="api-key",
            country_id=31,
            max_price=0.027,
            verification_code_wait_timeout=0,
        )
        service = SmsBowerService(config, http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="27657282036",
            attributes={"activation_id": "419892352"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 6, 29, 5, 48, tzinfo=UTC),
        )

        self.assertIsNone(code)
        self.assertEqual(
            session.requests[0]["params"],
            {
                "api_key": "api-key",
                "action": "getStatus",
                "id": "419892352",
            },
        )

    def test_get_latest_verification_code_polls_until_code_is_available(self) -> None:
        clock = FakePollClock()
        session = FakeSession(["STATUS_WAIT_CODE", "STATUS_OK: '123456'"])
        config = SmsBowerServiceConfig(
            base_url="https://smsbower.page/stubs/handler_api.php",
            api_key="api-key",
            country_id=31,
            max_price=0.027,
            verification_code_wait_timeout=10,
        )
        service = SmsBowerService(
            config,
            http_service=build_http_service(session),
            poll_interval_seconds=5,
            sleeper=clock.sleep,
            monotonic_clock=clock.monotonic,
        )
        mobile_number = SmsMobileNumber(
            mobile_number="27657282036",
            attributes={"activation_id": "419892352"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 6, 29, 5, 48, tzinfo=UTC),
        )

        self.assertEqual(code, "123456")
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(clock.sleeps, [5])

    def test_get_latest_verification_code_reads_status_ok_code(self) -> None:
        session = FakeSession(["STATUS_OK: '123456'"])
        service = SmsBowerService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="27657282036",
            attributes={"activation_id": "419892352"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 6, 29, 5, 48, tzinfo=UTC),
        )

        self.assertEqual(code, "123456")

    def test_get_latest_verification_code_reads_wait_retry_last_code(self) -> None:
        session = FakeSession(["STATUS_WAIT_RETRY:654321"])
        service = SmsBowerService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="27657282036",
            attributes={"activation_id": "419892352"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 6, 29, 5, 48, tzinfo=UTC),
        )

        self.assertEqual(code, "654321")

    def test_callback_cancels_activation_when_code_was_not_received(self) -> None:
        session = FakeSession(["ACCESS_CANCEL"])
        service = SmsBowerService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="27657282036",
            attributes={"activation_id": "419892352"},
        )

        service.callback(mobile_number, is_verification_code_received=False)

        self.assertEqual(
            session.requests[0]["params"],
            {
                "api_key": "api-key",
                "action": "setStatus",
                "status": 8,
                "id": "419892352",
            },
        )

    def test_callback_does_not_cancel_when_code_was_received(self) -> None:
        session = FakeSession([])
        service = SmsBowerService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="27657282036",
            attributes={"activation_id": "419892352"},
        )

        service.callback(mobile_number, is_verification_code_received=True)

        self.assertEqual(session.requests, [])

    def test_create_sms_service_supports_sms_bower_provider(self) -> None:
        service = create_sms_service(
            SmsServiceConfig(
                provider="sms_bower",
                provider_config={
                    "base_url": "https://smsbower.page/stubs/handler_api.php",
                    "api_key": "api-key",
                    "country_id": "31",
                    "max_price": "0.027",
                },
            ),
            http_service=build_http_service(FakeSession([])),
        )

        self.assertIsInstance(service, SmsBowerService)

    def test_create_sms_service_supports_smsbower_provider_alias(self) -> None:
        service = create_sms_service(
            SmsServiceConfig(
                provider="smsbower",
                provider_config={
                    "base_url": "https://smsbower.page/stubs/handler_api.php",
                    "api_key": "api-key",
                    "country_id": 31,
                    "max_price": 0.027,
                },
            ),
            http_service=build_http_service(FakeSession([])),
        )

        self.assertIsInstance(service, SmsBowerService)

    def test_sms_bower_config_accepts_numeric_strings(self) -> None:
        config = create_sms_bower_service_config(
            {
                "base_url": "https://smsbower.page/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": "31",
                "max_price": "0.027",
                "min_price": "0.01",
            }
        )

        self.assertEqual(config.country_id, 31)
        self.assertEqual(config.max_price, 0.027)
        self.assertEqual(config.min_price, 0.01)

    def test_sms_bower_config_uses_default_min_price(self) -> None:
        config = create_sms_bower_service_config(
            {
                "base_url": "https://smsbower.page/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": "31",
                "max_price": "0.027",
            }
        )

        self.assertEqual(config.min_price, 0)

    def test_sms_bower_config_reads_verification_code_wait_timeout(self) -> None:
        config = create_sms_bower_service_config(
            {
                "base_url": "https://smsbower.page/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": "31",
                "max_price": "0.027",
                "verification_code_wait_timeout": "75",
            }
        )

        self.assertEqual(config.verification_code_wait_timeout, 75)

    def test_sms_bower_config_uses_default_verification_code_wait_timeout(self) -> None:
        config = create_sms_bower_service_config(
            {
                "base_url": "https://smsbower.page/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": 31,
                "max_price": 0.027,
            }
        )

        self.assertEqual(config.verification_code_wait_timeout, 60)


if __name__ == "__main__":
    unittest.main()
