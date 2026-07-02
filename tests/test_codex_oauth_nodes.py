from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from account.account_service import Account
from account_export.account_export_service import (
    AccountExportOauthUrl,
    AccountExportSubmitResult,
)
from core.config import AppConfig, RegisterConfig
from email.email_service import EmailAccount
from register.nodes import (
    AddPhoneNumberNode,
    SelectCodexAccountNode,
    SubmitCodexConsentNode,
    WaitSmsVerificationCodeNode,
)
from register.register_flow import RegisterContext
from sms.sms_service import SmsMobileNumber


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 6, 29, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        parent=None,
        data_state: str | None = None,
        on_click=None,
        children: dict[str, list] | None = None,
    ) -> None:
        self._text = text
        self._parent = parent
        self._data_state = data_state
        self._on_click = on_click
        self._children = children or {}
        self.typed_texts: list[dict[str, object]] = []
        self.clicks: list[dict[str, object]] = []
        self.clear_count = 0

    async def query(self, selector: str, **kwargs):
        children = self._children.get(selector)
        if kwargs.get("find_all"):
            return children or []
        if children:
            return children[0]
        return None

    async def get_parent_element(self):
        return self._parent

    def get_attribute(self, name: str) -> str | None:
        if name == "data-state":
            return self._data_state
        return None

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

    def set_data_state(self, data_state: str) -> None:
        self._data_state = data_state


class FakeAccountExportService:
    def __init__(
        self,
        *,
        submit_result: AccountExportSubmitResult | None = None,
    ) -> None:
        self.oauth_url = AccountExportOauthUrl(
            url="https://auth.openai.com/oauth/authorize?state=state-token",
            state="state-token",
        )
        self.submit_result = submit_result or AccountExportSubmitResult(
            success=True,
            status="ok",
        )
        self.redirect_urls: list[str] = []

    def get_oauth_url(self) -> AccountExportOauthUrl:
        return self.oauth_url

    def submit_redirect_url(self, redirect_url: str) -> AccountExportSubmitResult:
        self.redirect_urls.append(redirect_url)
        return self.submit_result


class FakeEmailService:
    def __init__(self) -> None:
        self.callbacks: list[dict[str, object]] = []

    def callback(self, email_account: EmailAccount, is_email_used: bool) -> None:
        self.callbacks.append(
            {
                "email_account": email_account,
                "is_email_used": is_email_used,
            }
        )


class FakeSmsService:
    def __init__(self, codes: list[str | None] | None = None) -> None:
        self.mobile_number = SmsMobileNumber(
            mobile_number="79584123456",
            attributes={"activation_id": "activation-id"},
        )
        self.codes = codes or []
        self.callbacks: list[dict[str, object]] = []
        self.code_calls: list[dict[str, object]] = []
        self.excluded_activation_ids_calls: list[set[str]] = []

    def set_mobile_number(self, mobile_number: SmsMobileNumber) -> None:
        self.mobile_number = mobile_number

    def get_mobile_number(self, excluded_activation_ids=None) -> SmsMobileNumber:
        self.excluded_activation_ids_calls.append(set(excluded_activation_ids or ()))
        return self.mobile_number

    def get_latest_verification_code(
        self,
        mobile_number: SmsMobileNumber,
        sent_after: datetime,
    ) -> str | None:
        self.code_calls.append(
            {
                "mobile_number": mobile_number,
                "sent_after": sent_after,
            }
        )
        if not self.codes:
            return None
        return self.codes.pop(0)

    def callback(
        self,
        mobile_number: SmsMobileNumber,
        is_verification_code_received: bool,
    ) -> None:
        self.callbacks.append(
            {
                "mobile_number": mobile_number,
                "is_verification_code_received": is_verification_code_received,
            }
        )


@dataclass
class FakeAppContext:
    account_export_service: FakeAccountExportService | None = None
    email_service: FakeEmailService | None = None
    sms_service: FakeSmsService | None = None
    config: AppConfig = AppConfig()


def _account() -> Account:
    return Account(
        first_name="Jessica",
        last_name="Martin",
        age=28,
        password="Password123!",
        email_address="knuumnwxg@outlook.com",
    )


class SelectCodexAccountNodeTest(unittest.TestCase):
    def test_execute_selects_account_and_routes_to_phone_node(self) -> None:
        tab = FakeSelectAccountTab(next_url="https://auth.openai.com/add-phone")
        account_export_service = FakeAccountExportService()
        ctx = RegisterContext(
            app_context=FakeAppContext(account_export_service=account_export_service),
            state={"current_tab": tab, "account": _account()},
        )

        result = asyncio.run(SelectCodexAccountNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "codex_oauth_needs_phone")
        self.assertEqual(tab.visited_urls, [account_export_service.oauth_url.url])
        self.assertEqual(tab.account_button.clicks, [{"humanize": True}])
        self.assertEqual(
            result.data["codex_oauth_next_url"],
            "https://auth.openai.com/add-phone",
        )

    def test_execute_routes_directly_to_consent_node(self) -> None:
        tab = FakeSelectAccountTab(
            next_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        )
        ctx = RegisterContext(
            app_context=FakeAppContext(
                account_export_service=FakeAccountExportService(),
            ),
            state={"current_tab": tab, "account": _account()},
        )

        result = asyncio.run(SelectCodexAccountNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "codex_oauth_consent_ready")


class FakeSelectAccountTab:
    def __init__(self, next_url: str) -> None:
        self._current_url = "about:blank"
        self._next_url = next_url
        self.visited_urls: list[str] = []
        self.email_span = FakeElement("knuumnwxg@outlook.com")
        self.account_button = FakeElement(
            "Jessica Martin knuumnwxg@outlook.com",
            on_click=lambda: self._set_url(next_url),
            children={"span": [self.email_span]},
        )

    async def go_to(self, url: str) -> None:
        self.visited_urls.append(url)
        self._current_url = SelectCodexAccountNode.CHOOSE_ACCOUNT_URL

    async def query(self, selector: str, **kwargs):
        if selector == SelectCodexAccountNode.CHOOSE_ACCOUNT_BUTTON_SELECTOR:
            if kwargs.get("find_all"):
                return [self.account_button]
            return self.account_button
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        return self._current_url

    def _set_url(self, url: str) -> None:
        self._current_url = url


class AddPhoneNumberNodeTest(unittest.TestCase):
    def test_execute_gets_mobile_number_selects_sms_and_submits(self) -> None:
        tab = FakeAddPhoneTab()
        sms_service = FakeSmsService()
        account = _account()
        ctx = RegisterContext(
            app_context=FakeAppContext(sms_service=sms_service),
            state={"current_tab": tab, "account": account},
        )

        result = asyncio.run(AddPhoneNumberNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "phone_submitted")
        self.assertEqual(account.mobile, "+79584123456")
        self.assertEqual(
            tab.phone_input.typed_texts,
            [{"text": "+79584123456", "humanize": True}],
        )
        self.assertEqual(tab.whatsapp_label.clicks, [{"humanize": True}])
        self.assertEqual(tab.sms_label.clicks, [{"humanize": True}])
        self.assertEqual(tab.submit_button.clicks, [{"humanize": True}])
        self.assertIs(result.data["sms_mobile_number"], sms_service.mobile_number)
        self.assertEqual(sms_service.callbacks, [])

    def test_execute_cancels_mobile_number_when_page_returns_error(self) -> None:
        tab = FakeAddPhoneTab(error_text="手机号不可用")
        sms_service = FakeSmsService()
        ctx = RegisterContext(
            app_context=FakeAppContext(sms_service=sms_service),
            state={"current_tab": tab, "account": _account()},
        )

        result = asyncio.run(AddPhoneNumberNode().execute(ctx))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "phone_submit_error")
        self.assertEqual(result.error, "手机号不可用")
        self.assertEqual(
            sms_service.callbacks,
            [
                {
                    "mobile_number": sms_service.mobile_number,
                    "is_verification_code_received": False,
                }
            ],
        )

    def test_execute_requests_oauth_reauth_when_reusable_wait_exceeds_threshold(
        self,
    ) -> None:
        tab = FakeAddPhoneTab()
        mobile_number = SmsMobileNumber(
            mobile_number="79584123456",
            attributes={
                "activation_id": "activation-id",
                "reusable_activation_wait_seconds": 90,
            },
        )
        sms_service = FakeSmsService()
        sms_service.set_mobile_number(mobile_number)
        ctx = RegisterContext(
            app_context=FakeAppContext(
                sms_service=sms_service,
                config=AppConfig(
                    register=RegisterConfig(
                        oauth_reauth_wait_threshold_seconds=60,
                    ),
                ),
            ),
            state={"current_tab": tab, "account": _account()},
        )

        result = asyncio.run(AddPhoneNumberNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(
            result.status,
            AddPhoneNumberNode.OAUTH_REAUTH_REQUIRED_STATUS,
        )
        self.assertEqual(sms_service.excluded_activation_ids_calls, [set()])
        self.assertEqual(tab.submit_button.clicks, [])
        self.assertIs(
            result.data[AddPhoneNumberNode.SMS_MOBILE_NUMBER_STATE_KEY],
            mobile_number,
        )
        self.assertTrue(
            result.data[
                AddPhoneNumberNode.SMS_MOBILE_OAUTH_REAUTH_PENDING_STATE_KEY
            ]
        )

    def test_execute_uses_pending_mobile_number_after_oauth_reauth(self) -> None:
        async def fill_text_success(*args, **kwargs) -> None:
            return None

        tab = FakeAddPhoneTab()
        pending_mobile_number = SmsMobileNumber(
            mobile_number="79584123456",
            attributes={
                "activation_id": "activation-id",
                "reusable_activation_wait_seconds": 90,
            },
        )
        sms_service = FakeSmsService()
        ctx = RegisterContext(
            app_context=FakeAppContext(
                sms_service=sms_service,
                config=AppConfig(
                    register=RegisterConfig(
                        oauth_reauth_wait_threshold_seconds=60,
                    ),
                ),
            ),
            state={
                "current_tab": tab,
                "account": _account(),
                AddPhoneNumberNode.SMS_MOBILE_NUMBER_STATE_KEY: pending_mobile_number,
                AddPhoneNumberNode.SMS_MOBILE_OAUTH_REAUTH_PENDING_STATE_KEY: True,
            },
        )

        with patch(
            "register.nodes.add_phone_number_node.PydollClipboardInput.fill_text",
            new=fill_text_success,
        ):
            result = asyncio.run(AddPhoneNumberNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, AddPhoneNumberNode.SUCCESS_STATUS)
        self.assertEqual(sms_service.excluded_activation_ids_calls, [])
        self.assertEqual(tab.submit_button.clicks, [{"humanize": True}])
        self.assertIs(
            result.data[AddPhoneNumberNode.SMS_MOBILE_NUMBER_STATE_KEY],
            pending_mobile_number,
        )


class FakeAddPhoneTab:
    def __init__(self, error_text: str | None = None) -> None:
        self._current_url = "https://auth.openai.com/add-phone"
        self.error_text = error_text
        self.phone_input = FakeElement()
        self.sms_label = FakeElement(data_state="off")
        self.whatsapp_label = FakeElement(data_state="off")
        self.sms_input = FakeElement(parent=self.sms_label)
        self.whatsapp_input = FakeElement(parent=self.whatsapp_label)
        self.submit_button = FakeElement(on_click=self._handle_submit)
        self.error_element = FakeElement(error_text or "")

    async def query(self, selector: str, **kwargs):
        if selector == AddPhoneNumberNode.PHONE_INPUT_SELECTOR:
            return self.phone_input
        if selector == AddPhoneNumberNode.SMS_INPUT_SELECTOR:
            return self.sms_input
        if selector == AddPhoneNumberNode.WHATSAPP_INPUT_SELECTOR:
            return self.whatsapp_input
        if selector == AddPhoneNumberNode.SUBMIT_BUTTON_SELECTOR:
            return self.submit_button
        if selector == AddPhoneNumberNode.ERROR_MESSAGE_SELECTOR:
            if self.error_text is None:
                return None
            return self.error_element
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        return self._current_url

    def _handle_submit(self) -> None:
        if self.error_text is None:
            self._current_url = "https://auth.openai.com/phone-verification"


class WaitSmsVerificationCodeNodeTest(unittest.TestCase):
    def test_execute_gets_sms_code_from_service_and_reaches_consent(self) -> None:
        tab = FakeSmsCodeTab()
        sms_service = FakeSmsService(["654321"])
        ctx = _sms_context(tab, sms_service)
        node = WaitSmsVerificationCodeNode()

        result = asyncio.run(node.execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "phone_verified")
        self.assertEqual(len(sms_service.code_calls), 1)
        self.assertEqual(
            tab.code_input.typed_texts,
            [{"text": "654321", "humanize": True}],
        )
        self.assertEqual(tab.submit_button.clicks, [{"humanize": True}])
        self.assertEqual(
            sms_service.callbacks,
            [
                {
                    "mobile_number": sms_service.mobile_number,
                    "is_verification_code_received": True,
                }
            ],
        )

    def test_execute_resends_once_cancels_and_requests_codex_retry_when_sms_times_out(
        self,
    ) -> None:
        clock = Clock()
        tab = FakeSmsCodeTab()
        sms_service = FakeSmsService([None, None])
        ctx = _sms_context(tab, sms_service)
        node = WaitSmsVerificationCodeNode(
            now=clock.now,
        )

        result = asyncio.run(node.execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(
            result.status,
            WaitSmsVerificationCodeNode.RETRY_SELECT_CODEX_ACCOUNT_STATUS,
        )
        self.assertEqual(
            result.data[
                WaitSmsVerificationCodeNode.SMS_VERIFICATION_RETRY_COUNT_STATE_KEY
            ],
            1,
        )
        self.assertEqual(len(sms_service.code_calls), 2)
        self.assertEqual(tab.resend_button.clicks, [{"humanize": True}])
        self.assertEqual(
            sms_service.callbacks[-1],
            {
                "mobile_number": sms_service.mobile_number,
                "is_verification_code_received": False,
            },
        )

    def test_execute_fails_when_sms_timeout_retry_limit_is_reached(self) -> None:
        clock = Clock()
        tab = FakeSmsCodeTab()
        sms_service = FakeSmsService([None, None])
        ctx = _sms_context(
            tab,
            sms_service,
            config=AppConfig(
                register=RegisterConfig(sms_verification_retry_attempts=0),
            ),
        )
        node = WaitSmsVerificationCodeNode(
            now=clock.now,
        )

        result = asyncio.run(node.execute(ctx))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "sms_verification_code_timeout")
        self.assertEqual(len(sms_service.code_calls), 2)
        self.assertEqual(
            sms_service.callbacks[-1],
            {
                "mobile_number": sms_service.mobile_number,
                "is_verification_code_received": False,
            },
        )

    def test_execute_retries_oauth_without_resend_or_callback_when_phone_recently_used(
        self,
    ) -> None:
        async def fill_text_success(*args, **kwargs) -> None:
            return None

        tab = FakeSmsCodeTab(
            error_text=WaitSmsVerificationCodeNode.PHONE_RECENTLY_USED_ERROR_TEXT,
        )
        sms_service = FakeSmsService(["654321"])
        ctx = _sms_context(tab, sms_service)
        node = WaitSmsVerificationCodeNode()

        with patch(
            "register.nodes.wait_sms_verification_code_node."
            "PydollClipboardInput.fill_text",
            new=fill_text_success,
        ):
            result = asyncio.run(node.execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(
            result.status,
            WaitSmsVerificationCodeNode.RETRY_SELECT_CODEX_ACCOUNT_STATUS,
        )
        self.assertEqual(tab.resend_button.clicks, [])
        self.assertEqual(sms_service.callbacks, [])
        self.assertEqual(
            result.data[
                WaitSmsVerificationCodeNode.SMS_VERIFICATION_RETRY_COUNT_STATE_KEY
            ],
            1,
        )


class FakeSmsCodeTab:
    def __init__(
        self,
        error_text: str | None = None,
        submit_url: str = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    ) -> None:
        self._current_url = "https://auth.openai.com/phone-verification"
        self.error_text = error_text
        self._submit_url = submit_url
        self.code_input = FakeElement()
        self.submit_button = FakeElement(on_click=self._handle_submit)
        self.resend_button = FakeElement(on_click=self._handle_resend)
        self.error_element = FakeElement(error_text or "")

    async def query(self, selector: str, **kwargs):
        if selector == WaitSmsVerificationCodeNode.CODE_INPUT_SELECTOR:
            return self.code_input
        if selector == WaitSmsVerificationCodeNode.SUBMIT_BUTTON_SELECTOR:
            return self.submit_button
        if selector == WaitSmsVerificationCodeNode.RESEND_BUTTON_SELECTOR:
            return self.resend_button
        if selector == WaitSmsVerificationCodeNode.ERROR_MESSAGE_SELECTOR:
            if self.error_text is None:
                return None
            return self.error_element
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        return self._current_url

    def _handle_submit(self) -> None:
        if self.error_text is None:
            self._current_url = self._submit_url

    def _handle_resend(self) -> None:
        self.error_text = None


def _sms_context(
    tab: FakeSmsCodeTab,
    sms_service: FakeSmsService,
    config: AppConfig | None = None,
) -> RegisterContext:
    return RegisterContext(
        app_context=FakeAppContext(
            sms_service=sms_service,
            config=config or AppConfig(),
        ),
        state={
            "current_tab": tab,
            "sms_mobile_number": sms_service.mobile_number,
            "phone_submitted_at": datetime(2026, 6, 29, 12, tzinfo=UTC),
        },
    )


class SubmitCodexConsentNodeTest(unittest.TestCase):
    def test_execute_submits_redirect_url_and_calls_email_callback(self) -> None:
        tab = FakeConsentTab()
        account_export_service = FakeAccountExportService()
        email_service = FakeEmailService()
        email_account = EmailAccount("knuumnwxg@outlook.com")
        ctx = RegisterContext(
            app_context=FakeAppContext(
                account_export_service=account_export_service,
                email_service=email_service,
            ),
            state={"current_tab": tab, "email_account": email_account},
        )

        result = asyncio.run(SubmitCodexConsentNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "codex_account_exported")
        self.assertEqual(
            account_export_service.redirect_urls,
            ["http://localhost:1455/auth/callback?code=oauth-code"],
        )
        self.assertEqual(
            email_service.callbacks,
            [
                {
                    "email_account": email_account,
                    "is_email_used": True,
                }
            ],
        )


class FakeConsentTab:
    def __init__(self) -> None:
        self._current_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        self.submit_button = FakeElement(on_click=self._handle_submit)

    async def query(self, selector: str, **kwargs):
        if selector in (
            SubmitCodexConsentNode.SUBMIT_BUTTON_SELECTOR,
            SubmitCodexConsentNode.FALLBACK_BUTTON_SELECTOR,
        ):
            return self.submit_button
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        return self._current_url

    def _handle_submit(self) -> None:
        self._current_url = "http://localhost:1455/auth/callback?code=oauth-code"


if __name__ == "__main__":
    unittest.main()
