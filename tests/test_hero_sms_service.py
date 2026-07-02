from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path

from core.config import SmsActivationStoreConfig, SmsServiceConfig
from core.http_service import HttpService
from sms.activation_store import (
    SmsActivationRecord,
    SmsActivationStore,
    VerificationCodeEntry,
)
from sms.hero_sms_service import (
    HeroSmsService,
    HeroSmsServiceConfig,
    create_hero_sms_service_config,
)
from sms.sms_service import SmsMobileNumber, create_sms_service


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[dict | None]) -> None:
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


def build_config() -> HeroSmsServiceConfig:
    return HeroSmsServiceConfig(
        base_url="https://hero-sms.com/stubs/handler_api.php",
        api_key="api-key",
        country_id=6,
        max_price=12.5,
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


class FakeDateTimeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class HeroSmsServiceTest(unittest.TestCase):
    def test_get_mobile_number_returns_number_model_with_activation_id(self) -> None:
        session = FakeSession(
            [
                {
                    "activationId": "635468024",
                    "phoneNumber": "79584000000",
                    "activationCost": 12.5,
                    "currency": 840,
                    "countryCode": 6,
                    "countryPhoneCode": 62,
                    "canGetAnotherSms": True,
                    "activationTime": "2026-02-18T16:11:33+00:00",
                    "activationEndTime": "2026-02-18T18:11:23+00:00",
                    "activationOperator": "any",
                }
            ]
        )
        service = HeroSmsService(build_config(), http_service=build_http_service(session))

        mobile_number = service.get_mobile_number()

        self.assertEqual(mobile_number.mobile_number, "79584000000")
        self.assertEqual(mobile_number.get_attribute("provider"), "hero_sms")
        self.assertEqual(mobile_number.get_attribute("activation_id"), "635468024")
        self.assertEqual(
            session.requests[0],
            {
                "method": "GET",
                "url": "https://hero-sms.com/stubs/handler_api.php",
                "params": {
                    "action": "getNumberV2",
                    "service": HeroSmsService.SERVICE_CODE,
                    "country": 6,
                    "maxPrice": 12.5,
                    "api_key": "api-key",
                },
            },
        )

    def test_get_mobile_number_reuses_local_activation_before_buying_number(self) -> None:
        session = FakeSession([{"status": "ok"}])
        with TemporaryDirectory() as temp_dir:
            store = SmsActivationStore(Path(temp_dir) / "sms.db")
            store.upsert_activation(
                SmsActivationRecord(
                    provider=HeroSmsService.PROVIDER,
                    service_code=HeroSmsService.SERVICE_CODE,
                    mobile_number="79584000001",
                    activation_id="635468021",
                    activation_time=datetime(2022, 6, 1, 16, 59, tzinfo=UTC),
                    activation_end_time=datetime(2022, 6, 1, 18, 59, tzinfo=UTC),
                    can_get_another_sms=True,
                )
            )
            service = HeroSmsService(
                build_config(),
                http_service=build_http_service(session),
                activation_store=store,
                activation_store_config=SmsActivationStoreConfig(
                    reuse_min_interval_seconds=900,
                ),
                now=lambda: datetime(2022, 6, 1, 17, 15, tzinfo=UTC),
            )

            mobile_number = service.get_mobile_number()

        self.assertEqual(mobile_number.mobile_number, "79584000001")
        self.assertEqual(mobile_number.get_attribute("activation_id"), "635468021")
        self.assertTrue(mobile_number.get_attribute("reused_activation"))
        self.assertEqual(
            session.requests[0]["params"],
            {
                "action": "setStatus",
                "id": "635468021",
                "status": 3,
                "api_key": "api-key",
            },
        )

    def test_get_mobile_number_waits_for_soon_reusable_local_activation(self) -> None:
        session = FakeSession([{"status": "ok"}])
        clock = FakeDateTimeClock(datetime(2026, 7, 1, 10, 40, tzinfo=UTC))
        with TemporaryDirectory() as temp_dir:
            store = SmsActivationStore(Path(temp_dir) / "sms.db")
            store.upsert_activation(
                SmsActivationRecord(
                    provider=HeroSmsService.PROVIDER,
                    service_code=HeroSmsService.SERVICE_CODE,
                    mobile_number="79584000001",
                    activation_id="635468021",
                    activation_time=clock.now() - timedelta(minutes=5),
                    activation_end_time=clock.now() + timedelta(minutes=20),
                    can_get_another_sms=True,
                )
            )
            store.record_verification_code(
                provider=HeroSmsService.PROVIDER,
                activation_id="635468021",
                entry=VerificationCodeEntry(
                    code="123456",
                    received_at=clock.now() - timedelta(seconds=890),
                ),
            )
            store.mark_verification_code_usable(
                provider=HeroSmsService.PROVIDER,
                activation_id="635468021",
                usable_at=clock.now() - timedelta(seconds=890),
            )
            service = HeroSmsService(
                build_config(),
                http_service=build_http_service(session),
                activation_store=store,
                activation_store_config=SmsActivationStoreConfig(
                    reuse_min_interval_seconds=900,
                    wait_reusable_activation_enabled=True,
                ),
                sleeper=clock.sleep,
                now=clock.now,
            )

            mobile_number = service.get_mobile_number()

        self.assertEqual(mobile_number.mobile_number, "79584000001")
        self.assertEqual(clock.sleeps, [10.0])
        self.assertEqual(
            session.requests[0]["params"],
            {
                "action": "setStatus",
                "id": "635468021",
                "status": 3,
                "api_key": "api-key",
            },
        )

    def test_get_latest_verification_code_reads_sms_code_after_sent_time(self) -> None:
        session = FakeSession(
            [
                {
                    "verificationType": 2,
                    "sms": {
                        "dateTime": "2026-02-18 16:12:00",
                        "code": "123456",
                        "text": "your code is 123456",
                    },
                    "call": {
                        "dateTime": "2026-02-18 16:10:00",
                        "code": "99999",
                    },
                }
            ]
        )
        service = HeroSmsService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="79584000000",
            attributes={"activation_id": "635468024"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 2, 18, 16, 11, tzinfo=UTC),
        )

        self.assertEqual(code, "123456")
        self.assertEqual(
            session.requests[0]["params"],
            {
                "action": "getStatusV2",
                "id": "635468024",
                "api_key": "api-key",
            },
        )

    def test_get_latest_verification_code_polls_until_code_is_available(self) -> None:
        clock = FakePollClock()
        session = FakeSession(
            [
                {
                    "sms": {
                        "dateTime": "0000-00-00 00:00:00",
                        "code": "",
                    },
                },
                {
                    "sms": {
                        "dateTime": "2026-02-18 16:12:00",
                        "code": "123456",
                    },
                },
            ]
        )
        config = HeroSmsServiceConfig(
            base_url="https://hero-sms.com/stubs/handler_api.php",
            api_key="api-key",
            country_id=6,
            max_price=12.5,
            verification_code_wait_timeout=10,
        )
        service = HeroSmsService(
            config,
            http_service=build_http_service(session),
            poll_interval_seconds=5,
            sleeper=clock.sleep,
            monotonic_clock=clock.monotonic,
        )
        mobile_number = SmsMobileNumber(
            mobile_number="79584000000",
            attributes={"activation_id": "635468024"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 2, 18, 16, 11, tzinfo=UTC),
        )

        self.assertEqual(code, "123456")
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(clock.sleeps, [5])

    def test_get_latest_verification_code_returns_none_after_timeout(self) -> None:
        session = FakeSession(
            [
                {
                    "sms": {
                        "dateTime": "0000-00-00 00:00:00",
                        "code": "",
                    },
                }
            ]
        )
        config = HeroSmsServiceConfig(
            base_url="https://hero-sms.com/stubs/handler_api.php",
            api_key="api-key",
            country_id=6,
            max_price=12.5,
            verification_code_wait_timeout=0,
        )
        service = HeroSmsService(config, http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="79584000000",
            attributes={"activation_id": "635468024"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 2, 18, 16, 11, tzinfo=UTC),
        )

        self.assertIsNone(code)
        self.assertEqual(len(session.requests), 1)

    def test_get_latest_verification_code_accepts_code_without_valid_datetime(self) -> None:
        session = FakeSession(
            [
                {
                    "sms": {
                        "dateTime": "0000-00-00 00:00:00",
                        "code": "654321",
                        "text": "your code is 654321",
                    },
                }
            ]
        )
        service = HeroSmsService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="79584000000",
            attributes={"activation_id": "635468024"},
        )

        code = service.get_latest_verification_code(
            mobile_number,
            sent_after=datetime(2026, 2, 18, 16, 11, tzinfo=UTC),
        )

        self.assertEqual(code, "654321")

    def test_callback_cancels_activation_when_code_was_not_received(self) -> None:
        session = FakeSession([{"status": "ok"}])
        service = HeroSmsService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="79584000000",
            attributes={"activation_id": "635468024"},
        )

        service.callback(mobile_number, is_verification_code_received=False)

        self.assertEqual(
            session.requests[0]["params"],
            {
                "action": "setStatus",
                "id": "635468024",
                "status": 8,
                "api_key": "api-key",
            },
        )

    def test_callback_does_not_cancel_when_code_was_received(self) -> None:
        session = FakeSession([])
        service = HeroSmsService(build_config(), http_service=build_http_service(session))
        mobile_number = SmsMobileNumber(
            mobile_number="79584000000",
            attributes={"activation_id": "635468024"},
        )

        service.callback(mobile_number, is_verification_code_received=True)

        self.assertEqual(session.requests, [])

    def test_create_sms_service_supports_hero_sms_provider(self) -> None:
        service = create_sms_service(
            SmsServiceConfig(
                provider="hero_sms",
                provider_config={
                    "base_url": "https://hero-sms.com/stubs/handler_api.php",
                    "api_key": "api-key",
                    "country_id": 6,
                    "max_price": 12.5,
                },
            ),
            http_service=build_http_service(FakeSession([])),
        )

        self.assertIsInstance(service, HeroSmsService)

    def test_hero_sms_config_accepts_numeric_string_country_id(self) -> None:
        config = create_hero_sms_service_config(
            {
                "base_url": "https://hero-sms.com/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": "31",
                "max_price": 0.05,
            }
        )

        self.assertEqual(config.country_id, 31)

    def test_hero_sms_config_reads_verification_code_wait_timeout(self) -> None:
        config = create_hero_sms_service_config(
            {
                "base_url": "https://hero-sms.com/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": 6,
                "max_price": 12.5,
                "verification_code_wait_timeout": 90,
            }
        )

        self.assertEqual(config.verification_code_wait_timeout, 90)

    def test_hero_sms_config_uses_default_verification_code_wait_timeout(self) -> None:
        config = create_hero_sms_service_config(
            {
                "base_url": "https://hero-sms.com/stubs/handler_api.php",
                "api_key": "api-key",
                "country_id": 6,
                "max_price": 12.5,
            }
        )

        self.assertEqual(config.verification_code_wait_timeout, 125)


if __name__ == "__main__":
    unittest.main()
