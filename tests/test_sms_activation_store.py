from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sms.activation_store import (
    SmsActivationRecord,
    SmsActivationStore,
    VerificationCodeEntry,
)


class SmsActivationStoreTest(unittest.TestCase):
    def test_reusable_filter_uses_last_usable_time_not_last_received_time(self) -> None:
        now = datetime(2026, 7, 1, 10, 40, tzinfo=UTC)
        with TemporaryDirectory() as temp_dir:
            store = SmsActivationStore(Path(temp_dir) / "sms.db")
            store.upsert_activation(
                SmsActivationRecord(
                    provider="hero_sms",
                    service_code="dr",
                    mobile_number="27628726006",
                    activation_id="activation-id",
                    activation_time=now - timedelta(minutes=5),
                    activation_end_time=now + timedelta(minutes=15),
                    can_get_another_sms=True,
                )
            )
            store.record_verification_code(
                provider="hero_sms",
                activation_id="activation-id",
                entry=VerificationCodeEntry(
                    code="123456",
                    received_at=now - timedelta(seconds=30),
                ),
            )

            reusable_records = store.list_reusable_activations(
                provider="hero_sms",
                service_code="dr",
                excluded_activation_ids=None,
                now=now,
                reuse_min_interval_seconds=900,
            )

            self.assertEqual(
                [record.activation_id for record in reusable_records],
                ["activation-id"],
            )

            store.mark_verification_code_usable(
                provider="hero_sms",
                activation_id="activation-id",
                usable_at=now - timedelta(seconds=30),
            )

            reusable_records = store.list_reusable_activations(
                provider="hero_sms",
                service_code="dr",
                excluded_activation_ids=None,
                now=now,
                reuse_min_interval_seconds=900,
            )

            self.assertEqual(reusable_records, [])

            store.mark_verification_code_usable(
                provider="hero_sms",
                activation_id="activation-id",
                usable_at=now - timedelta(minutes=20),
            )

            reusable_records = store.list_reusable_activations(
                provider="hero_sms",
                service_code="dr",
                excluded_activation_ids=None,
                now=now,
                reuse_min_interval_seconds=900,
            )

            self.assertEqual(
                [record.activation_id for record in reusable_records],
                ["activation-id"],
            )

    def test_reusable_filter_requires_enough_remaining_activation_time(self) -> None:
        now = datetime(2026, 7, 1, 10, 40, tzinfo=UTC)
        with TemporaryDirectory() as temp_dir:
            store = SmsActivationStore(Path(temp_dir) / "sms.db")
            store.upsert_activation(
                SmsActivationRecord(
                    provider="hero_sms",
                    service_code="dr",
                    mobile_number="27628726006",
                    activation_id="too-close",
                    activation_time=now - timedelta(minutes=5),
                    activation_end_time=now + timedelta(seconds=30),
                    can_get_another_sms=True,
                )
            )
            store.upsert_activation(
                SmsActivationRecord(
                    provider="hero_sms",
                    service_code="dr",
                    mobile_number="27628726007",
                    activation_id="enough-time",
                    activation_time=now - timedelta(minutes=5),
                    activation_end_time=now + timedelta(minutes=5),
                    can_get_another_sms=True,
                )
            )

            reusable_records = store.list_reusable_activations(
                provider="hero_sms",
                service_code="dr",
                excluded_activation_ids=None,
                now=now,
                reuse_min_interval_seconds=900,
                min_remaining_seconds=125,
            )

            self.assertEqual(
                [record.activation_id for record in reusable_records],
                ["enough-time"],
            )

    def test_find_next_waitable_reusable_activation_returns_soonest_record(self) -> None:
        now = datetime(2026, 7, 1, 10, 40, tzinfo=UTC)
        with TemporaryDirectory() as temp_dir:
            store = SmsActivationStore(Path(temp_dir) / "sms.db")
            store.upsert_activation(
                SmsActivationRecord(
                    provider="hero_sms",
                    service_code="dr",
                    mobile_number="27628726006",
                    activation_id="soon",
                    activation_time=now - timedelta(minutes=5),
                    activation_end_time=now + timedelta(minutes=20),
                    can_get_another_sms=True,
                )
            )
            store.mark_verification_code_usable(
                provider="hero_sms",
                activation_id="soon",
                usable_at=now - timedelta(minutes=14),
            )
            store.upsert_activation(
                SmsActivationRecord(
                    provider="hero_sms",
                    service_code="dr",
                    mobile_number="27628726007",
                    activation_id="later",
                    activation_time=now - timedelta(minutes=5),
                    activation_end_time=now + timedelta(minutes=25),
                    can_get_another_sms=True,
                )
            )
            store.mark_verification_code_usable(
                provider="hero_sms",
                activation_id="later",
                usable_at=now - timedelta(minutes=10),
            )

            waitable_record = store.find_next_waitable_reusable_activation(
                provider="hero_sms",
                service_code="dr",
                excluded_activation_ids=None,
                now=now,
                reuse_min_interval_seconds=900,
                min_remaining_seconds=125,
            )

            self.assertIsNotNone(waitable_record)
            assert waitable_record is not None
            self.assertEqual(waitable_record.record.activation_id, "soon")
            self.assertEqual(waitable_record.wait_seconds, 60)

    def test_find_next_waitable_reusable_activation_skips_expiring_record(self) -> None:
        now = datetime(2026, 7, 1, 10, 40, tzinfo=UTC)
        with TemporaryDirectory() as temp_dir:
            store = SmsActivationStore(Path(temp_dir) / "sms.db")
            store.upsert_activation(
                SmsActivationRecord(
                    provider="hero_sms",
                    service_code="dr",
                    mobile_number="27628726006",
                    activation_id="expires-too-soon",
                    activation_time=now - timedelta(minutes=5),
                    activation_end_time=now + timedelta(seconds=90),
                    can_get_another_sms=True,
                )
            )
            store.mark_verification_code_usable(
                provider="hero_sms",
                activation_id="expires-too-soon",
                usable_at=now - timedelta(minutes=14),
            )

            waitable_record = store.find_next_waitable_reusable_activation(
                provider="hero_sms",
                service_code="dr",
                excluded_activation_ids=None,
                now=now,
                reuse_min_interval_seconds=900,
                min_remaining_seconds=125,
            )

            self.assertIsNone(waitable_record)


if __name__ == "__main__":
    unittest.main()
