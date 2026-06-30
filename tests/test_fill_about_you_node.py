from __future__ import annotations

import asyncio
import unittest

from account.account_service import Account
from register.nodes import FillAboutYouNode
from register.register_flow import RegisterContext


class FakeElement:
    def __init__(self, text: str = "", on_click=None) -> None:
        self._text = text
        self._on_click = on_click
        self.typed_texts: list[dict[str, object]] = []
        self.clicks: list[dict[str, object]] = []

    async def type_text(self, text: str, humanize: bool = False) -> None:
        self.typed_texts.append({"text": text, "humanize": humanize})

    async def click(self, *, humanize: bool = False) -> None:
        self.clicks.append({"humanize": humanize})
        if self._on_click is not None:
            self._on_click()

    @property
    async def text(self) -> str:
        return self._text


class FakeTab:
    def __init__(
        self,
        final_url: str = "https://chatgpt.com/",
        error_text: str | None = None,
    ) -> None:
        self._current_url = "https://auth.openai.com/about-you"
        self._final_url = final_url
        self._submit_clicked = False
        self.error_text = error_text
        self.name_input = FakeElement()
        self.age_input = FakeElement()
        self.submit_button = FakeElement(on_click=self._submit)
        self.ready_dialog = FakeElement()
        self.error_element = FakeElement(error_text or "")
        self.query_calls: list[dict[str, object]] = []

    async def query(self, selector: str, **kwargs) -> FakeElement | None:
        self.query_calls.append({"selector": selector, **kwargs})
        if selector == FillAboutYouNode.NAME_INPUT_SELECTOR:
            return self.name_input
        if selector == FillAboutYouNode.AGE_INPUT_SELECTOR:
            return self.age_input
        if selector == FillAboutYouNode.SUBMIT_BUTTON_SELECTOR:
            return self.submit_button
        if selector == FillAboutYouNode.READY_DIALOG_SELECTOR:
            return self.ready_dialog
        if selector == FillAboutYouNode.ERROR_MESSAGE_SELECTOR:
            if self.error_text is None:
                return None
            return self.error_element
        raise AssertionError(f"未知选择器: {selector}")

    @property
    async def current_url(self) -> str:
        if self._submit_clicked:
            self._current_url = self._final_url
        return self._current_url

    def _submit(self) -> None:
        self._submit_clicked = True


def _account() -> Account:
    return Account(
        first_name="James",
        last_name="Smith",
        age=28,
        password="Password123!",
        email_address="user@example.com",
    )


class FillAboutYouNodeTest(unittest.TestCase):
    def test_execute_fills_profile_and_waits_ready_dialog(self) -> None:
        tab = FakeTab()
        ctx = RegisterContext(
            state={
                "current_tab": tab,
                "account": _account(),
                "about_you_name_input": tab.name_input,
                "about_you_age_input": tab.age_input,
            }
        )

        result = asyncio.run(FillAboutYouNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "about_you_submitted")
        self.assertEqual(
            tab.name_input.typed_texts,
            [{"text": "James Smith", "humanize": True}],
        )
        self.assertEqual(tab.age_input.typed_texts, [{"text": "28", "humanize": True}])
        self.assertEqual(tab.submit_button.clicks, [{"humanize": True}])
        self.assertIs(result.data["chatgpt_ready_dialog"], tab.ready_dialog)
        self.assertEqual(result.data["chatgpt_final_url"], "https://chatgpt.com/")

    def test_execute_queries_inputs_when_context_does_not_have_them(self) -> None:
        tab = FakeTab()
        ctx = RegisterContext(state={"current_tab": tab, "account": _account()})

        result = asyncio.run(FillAboutYouNode().execute(ctx))

        self.assertTrue(result.success)
        self.assertEqual(
            tab.query_calls[0],
            {
                "selector": FillAboutYouNode.NAME_INPUT_SELECTOR,
                "timeout": 10,
                "raise_exc": True,
            },
        )
        self.assertEqual(
            tab.query_calls[1],
            {
                "selector": FillAboutYouNode.AGE_INPUT_SELECTOR,
                "timeout": 10,
                "raise_exc": True,
            },
        )

    def test_execute_fails_when_final_url_is_unexpected(self) -> None:
        tab = FakeTab(final_url="https://example.com/")
        ctx = RegisterContext(
            state={
                "current_tab": tab,
                "account": _account(),
                "about_you_name_input": tab.name_input,
                "about_you_age_input": tab.age_input,
            }
        )

        result = asyncio.run(FillAboutYouNode().execute(ctx))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "chatgpt_unexpected_final_url")
        self.assertIn("URL 不符合预期", result.error or "")
        self.assertIn(
            FillAboutYouNode.READY_DIALOG_SELECTOR,
            [call["selector"] for call in tab.query_calls],
        )

    def test_execute_fails_when_account_create_error_appears(self) -> None:
        tab = FakeTab(error_text="无法创建你的帐户，请稍后再试")
        ctx = RegisterContext(
            state={
                "current_tab": tab,
                "account": _account(),
                "about_you_name_input": tab.name_input,
                "about_you_age_input": tab.age_input,
            }
        )

        result = asyncio.run(FillAboutYouNode().execute(ctx))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "account_create_failed")
        self.assertIn("无法创建你的帐户", result.error or "")


if __name__ == "__main__":
    unittest.main()
