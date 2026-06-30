from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.config import AppConfig, RegisterConfig
from email.email_service import EmailAccount, EmailMessage
from register.nodes import WaitEmailVerificationCodeNode
from register.register_flow import RegisterContext


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 6, 29, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeElement:
    def __init__(self, text: str = "", on_click=None) -> None:
        self._text = text
        self._on_click = on_click
        self.typed_texts: list[dict[str, object]] = []
        self.clicks: list[dict[str, object]] = []
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1

    async def type_text(self, text: str, humanize: bool = False) -> None:
        self.typed_texts.append({"text": text, "humanize": humanize})

    async def click(self, *, humanize: bool = False) -> None:
        self.clicks.append({"humanize": humanize})
        if self._on_click is not None:
            self._on_click()

    @property
    async def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text


class FakeTab:
    def __init__(
        self,
        validate_url: str = "https://auth.openai.com/about-you",
        show_profile_button_after_validate: bool = False,
    ) -> None:
        self._current_url = "https://auth.openai.com/email-verification"
        self._validate_url = validate_url
        self._show_profile_button_after_validate = show_profile_button_after_validate
        self.profile_button_visible = False
        self.error_text: str | None = None
        self.code_input = FakeElement()
        self.validate_button = FakeElement(on_click=self._handle_validate)
        self.resend_button = FakeElement(on_click=self._handle_resend)
        self.error_element = FakeElement()
        self.name_input = FakeElement()
        self.age_input = FakeElement()
        self.profile_button = FakeElement()
        self.query_calls: list[dict[str, object]] = []
        self.validate_click_count = 0

    async def query(self, selector: str, **kwargs) -> FakeElement | None:
        self.query_calls.append({"selector": selector, **kwargs})
        if selector == WaitEmailVerificationCodeNode.CODE_INPUT_SELECTOR:
            return self.code_input
        if selector == WaitEmailVerificationCodeNode.VALIDATE_BUTTON_SELECTOR:
            return self.validate_button
        if selector == WaitEmailVerificationCodeNode.RESEND_BUTTON_SELECTOR:
            return self.resend_button
        if selector == WaitEmailVerificationCodeNode.ERROR_MESSAGE_SELECTOR:
            if self.error_text is None:
                return None
            self.error_element.set_text(self.error_text)
            return self.error_element
        if selector == WaitEmailVerificationCodeNode.ABOUT_YOU_NAME_INPUT_SELECTOR:
            if self._current_url != "https://auth.openai.com/about-you":
                return None
            return self.name_input
        if selector == WaitEmailVerificationCodeNode.ABOUT_YOU_AGE_INPUT_SELECTOR:
            if self._current_url != "https://auth.openai.com/about-you":
                return None
            return self.age_input
        if selector == WaitEmailVerificationCodeNode.CHATGPT_PROFILE_BUTTON_SELECTOR:
            if self.profile_button_visible:
                return self.profile_button
            return None
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        return self._current_url

    def _handle_validate(self) -> None:
        self.validate_click_count += 1
        if self.error_text is None:
            self._current_url = self._validate_url
            self.profile_button_visible = self._show_profile_button_after_validate

    def _handle_resend(self) -> None:
        self.error_text = None


class FakeEmailService:
    def __init__(self, messages: list[EmailMessage | None]) -> None:
        self._messages = messages
        self.calls: list[dict[str, object]] = []

    def search_first_email(
        self,
        email_account: EmailAccount,
        sent_after: datetime,
    ) -> EmailMessage | None:
        self.calls.append(
            {
                "email_account": email_account,
                "sent_after": sent_after,
            }
        )
        if not self._messages:
            return None
        return self._messages.pop(0)


@dataclass
class FakeAppContext:
    email_service: FakeEmailService
    config: AppConfig


def _message(code: str) -> EmailMessage:
    return EmailMessage(
        email_address="user@example.com",
        sender="OpenAI",
        subject="Verify",
        sent_at=datetime(2026, 6, 29, 12, tzinfo=UTC),
        verification_code=code,
    )


def _context(tab: FakeTab, email_service: FakeEmailService) -> RegisterContext:
    return RegisterContext(
        app_context=FakeAppContext(
            email_service=email_service,
            config=AppConfig(register=RegisterConfig(verification_code_wait_timeout=15)),
        ),
        state={
            "current_tab": tab,
            "email_account": EmailAccount("user@example.com"),
            "email_submitted_at": datetime(2026, 6, 29, 12, tzinfo=UTC),
            "email_verification_code_input": tab.code_input,
        },
    )


class WaitEmailVerificationCodeNodeTest(unittest.TestCase):
    def test_execute_polls_code_and_reaches_about_you(self) -> None:
        clock = Clock()
        tab = FakeTab()
        email_service = FakeEmailService([None, _message("123456")])
        node = WaitEmailVerificationCodeNode(
            poll_interval_seconds=5,
            sleeper=clock.sleep,
            now=clock.now,
        )

        result = asyncio.run(node.execute(_context(tab, email_service)))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "email_verified")
        self.assertEqual(tab.code_input.clear_count, 1)
        self.assertEqual(
            tab.code_input.typed_texts,
            [{"text": "123456", "humanize": True}],
        )
        self.assertEqual(tab.validate_button.clicks, [{"humanize": True}])
        self.assertIs(result.data["about_you_name_input"], tab.name_input)
        self.assertIs(result.data["about_you_age_input"], tab.age_input)
        self.assertEqual(
            result.data["about_you_url"],
            "https://auth.openai.com/about-you",
        )

    def test_execute_resends_when_code_is_invalid(self) -> None:
        clock = Clock()
        tab = FakeTab()
        tab.error_text = "代码不正确"
        email_service = FakeEmailService([_message("111111"), _message("222222")])
        node = WaitEmailVerificationCodeNode(
            poll_interval_seconds=5,
            sleeper=clock.sleep,
            now=clock.now,
        )

        result = asyncio.run(node.execute(_context(tab, email_service)))

        self.assertTrue(result.success)
        self.assertEqual(
            tab.code_input.typed_texts,
            [
                {"text": "111111", "humanize": True},
                {"text": "222222", "humanize": True},
            ],
        )
        self.assertEqual(tab.resend_button.clicks, [{"humanize": True}])
        self.assertEqual(tab.validate_click_count, 2)

    def test_execute_routes_to_oauth_when_email_verification_enters_chatgpt(self) -> None:
        clock = Clock()
        tab = FakeTab(
            validate_url="https://chatgpt.com/",
            show_profile_button_after_validate=True,
        )
        email_service = FakeEmailService([_message("123456")])
        node = WaitEmailVerificationCodeNode(
            poll_interval_seconds=5,
            sleeper=clock.sleep,
            now=clock.now,
        )

        result = asyncio.run(node.execute(_context(tab, email_service)))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "email_verified_chatgpt_ready")
        self.assertEqual(
            tab.code_input.typed_texts,
            [{"text": "123456", "humanize": True}],
        )
        self.assertEqual(result.data["about_you_url"], "https://chatgpt.com/")

    def test_execute_fails_when_account_create_error_appears(self) -> None:
        clock = Clock()
        tab = FakeTab()
        tab.error_text = "无法创建你的帐户，请稍后再试"
        email_service = FakeEmailService([_message("123456")])
        node = WaitEmailVerificationCodeNode(sleeper=clock.sleep, now=clock.now)

        result = asyncio.run(node.execute(_context(tab, email_service)))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "account_create_failed")
        self.assertIn("无法创建你的帐户", result.error or "")

    def test_execute_times_out_when_code_never_arrives(self) -> None:
        clock = Clock()
        tab = FakeTab()
        email_service = FakeEmailService([None, None, None, None])
        node = WaitEmailVerificationCodeNode(
            poll_interval_seconds=5,
            sleeper=clock.sleep,
            now=clock.now,
        )

        result = asyncio.run(node.execute(_context(tab, email_service)))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "email_verification_code_timeout")
        self.assertEqual(tab.validate_button.clicks, [])


if __name__ == "__main__":
    unittest.main()
