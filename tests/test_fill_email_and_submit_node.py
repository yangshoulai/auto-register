from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import datetime

from account.account_service import Account
from email.email_service import EmailAccount
from register.nodes import FillEmailAndSubmitNode
from register.register_flow import RegisterContext


class FakeElement:
    def __init__(self, on_click=None) -> None:
        self.typed_texts: list[dict[str, object]] = []
        self.clicks: list[dict[str, object]] = []
        self._on_click = on_click

    async def type_text(self, text: str, humanize: bool = False) -> None:
        self.typed_texts.append({"text": text, "humanize": humanize})

    async def click(self, *, humanize: bool = False) -> None:
        self.clicks.append({"humanize": humanize})
        if self._on_click is not None:
            self._on_click()


class FakeTab:
    def __init__(
        self,
        verification_url: str,
        *,
        email_dialog_visible: bool = True,
        page_email_available: bool = False,
    ) -> None:
        self._current_url = "https://chatgpt.com/"
        self.email_dialog_visible = email_dialog_visible
        self.page_email_available = page_email_available
        self.email_input = FakeElement()
        self.submit_button = FakeElement(on_click=lambda: self._set_url(verification_url))
        self.page_email_input = FakeElement()
        self.page_submit_button = FakeElement(
            on_click=lambda: self._set_url(verification_url)
        )
        self.code_input = FakeElement()
        self.sms_code_input = FakeElement()
        self.query_calls: list[dict[str, object]] = []

    async def query(self, selector: str, **kwargs) -> FakeElement | None:
        self.query_calls.append({"selector": selector, **kwargs})
        if selector == FillEmailAndSubmitNode.EMAIL_INPUT_SELECTOR:
            if not self.email_dialog_visible:
                if kwargs.get("raise_exc", True):
                    raise AssertionError("邮箱输入框已不存在")
                return None
            return self.email_input
        if selector == FillEmailAndSubmitNode.PAGE_EMAIL_INPUT_SELECTOR:
            if self.page_email_available:
                return self.page_email_input
            if kwargs.get("raise_exc", True):
                raise AssertionError("页面邮箱输入框不存在")
            return None
        if selector == FillEmailAndSubmitNode.SUBMIT_BUTTON_SELECTOR:
            if not self.email_dialog_visible:
                if kwargs.get("raise_exc", True):
                    raise AssertionError("弹窗提交按钮不存在")
                return None
            return self.submit_button
        if selector == FillEmailAndSubmitNode.PAGE_SUBMIT_BUTTON_SELECTOR:
            if self.page_email_available:
                return self.page_submit_button
            if kwargs.get("raise_exc", True):
                raise AssertionError("页面提交按钮不存在")
            return None
        if selector == FillEmailAndSubmitNode.VERIFICATION_CODE_INPUT_SELECTOR:
            if self._current_url.startswith("https://chatgpt.com"):
                if kwargs.get("raise_exc", True):
                    raise AssertionError("验证码输入框不存在")
                return None
            if FillEmailAndSubmitNode.SMS_VERIFICATION_URL_PART in self._current_url:
                if kwargs.get("raise_exc", True):
                    raise AssertionError("邮箱验证码输入框不存在")
                return None
            return self.code_input
        if selector == FillEmailAndSubmitNode.SMS_VERIFICATION_CODE_INPUT_SELECTOR:
            if FillEmailAndSubmitNode.SMS_VERIFICATION_URL_PART in self._current_url:
                return self.sms_code_input
            if kwargs.get("raise_exc", True):
                raise AssertionError("短信验证码输入框不存在")
            return None
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        return self._current_url

    def _set_url(self, url: str) -> None:
        self._current_url = url
        self.email_dialog_visible = False


class FakeAccountService:
    def __init__(self) -> None:
        self.account = Account(
            first_name="James",
            last_name="Smith",
            age=28,
            password="Password123!",
        )
        self.calls = 0

    def create_account(self) -> Account:
        self.calls += 1
        return self.account


class FakeEmailService:
    def __init__(self) -> None:
        self.email_account = EmailAccount("user@example.com", {"mode": "temp"})
        self.calls = 0

    def generate_email_address(self) -> EmailAccount:
        self.calls += 1
        return self.email_account


@dataclass
class FakeAppContext:
    account_service: FakeAccountService
    email_service: FakeEmailService


class FillEmailAndSubmitNodeTest(unittest.TestCase):
    def test_execute_creates_account_email_fills_input_and_submits(self) -> None:
        tab = FakeTab("https://auth.openai.com/email-verification")
        account_service = FakeAccountService()
        email_service = FakeEmailService()
        ctx = RegisterContext(
            app_context=FakeAppContext(account_service, email_service),
            state={
                "current_tab": tab,
                "chatgpt_email_input": tab.email_input,
            },
        )

        result = asyncio.run(FillEmailAndSubmitNode().execute(ctx))
        ctx.update_values(result.data)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "email_submitted")
        self.assertEqual(account_service.calls, 1)
        self.assertEqual(email_service.calls, 1)
        self.assertEqual(account_service.account.email_address, "user@example.com")
        self.assertEqual(
            tab.email_input.typed_texts,
            [{"text": "user@example.com", "humanize": True}],
        )
        self.assertEqual(tab.submit_button.clicks, [{"humanize": True}])
        self.assertIs(ctx.get_value("account"), account_service.account)
        self.assertIs(ctx.get_value("email_account"), email_service.email_account)
        self.assertIsInstance(ctx.get_value("email_submitted_at"), datetime)
        self.assertIs(ctx.get_value("email_verification_code_input"), tab.code_input)
        self.assertEqual(
            ctx.get_value("email_verification_url"),
            "https://auth.openai.com/email-verification",
        )

    def test_execute_queries_email_input_when_context_does_not_have_it(self) -> None:
        tab = FakeTab("https://auth.openai.com/email-verification")
        ctx = RegisterContext(
            app_context=FakeAppContext(FakeAccountService(), FakeEmailService()),
            state={"current_tab": tab},
        )

        result = asyncio.run(FillEmailAndSubmitNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(
            tab.query_calls[0],
            {
                "selector": FillEmailAndSubmitNode.EMAIL_INPUT_SELECTOR,
                "timeout": 5,
                "raise_exc": False,
            },
        )

    def test_execute_supports_auth_login_page_email_form(self) -> None:
        tab = FakeTab(
            "https://auth.openai.com/phone-verification",
            email_dialog_visible=False,
            page_email_available=True,
        )
        account_service = FakeAccountService()
        email_service = FakeEmailService()
        ctx = RegisterContext(
            app_context=FakeAppContext(account_service, email_service),
            state={"current_tab": tab},
        )

        result = asyncio.run(FillEmailAndSubmitNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "email_submitted_sms_verification_ready")
        self.assertEqual(
            tab.page_email_input.typed_texts,
            [{"text": "user@example.com", "humanize": True}],
        )
        self.assertEqual(tab.page_submit_button.clicks, [{"humanize": True}])
        self.assertIs(result.data["account"], account_service.account)
        self.assertIs(result.data["email_account"], email_service.email_account)
        self.assertIn("phone_submitted_at", result.data)

    def test_execute_fails_when_verification_url_is_unexpected(self) -> None:
        tab = FakeTab("https://auth.openai.com/other")
        ctx = RegisterContext(
            app_context=FakeAppContext(FakeAccountService(), FakeEmailService()),
            state={
                "current_tab": tab,
                "chatgpt_email_input": tab.email_input,
            },
        )

        result = asyncio.run(FillEmailAndSubmitNode().execute(ctx))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "email_verification_unexpected_url")
        self.assertIn("URL 不符合预期", result.error or "")
        self.assertIs(result.data["email_verification_code_input"], tab.code_input)

    def test_execute_fails_without_app_context(self) -> None:
        result = asyncio.run(FillEmailAndSubmitNode().execute(RegisterContext()))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "email_submit_failed")
        self.assertIn("缺少 AppContext", result.error or "")


if __name__ == "__main__":
    unittest.main()
