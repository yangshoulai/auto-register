from __future__ import annotations

import asyncio
import unittest

from register.browser_context import (
    PydollBrowserContextInitializer,
    PYDOLL_BROWSER_CREATED_STATE_KEY,
)
from register.nodes import OpenChatGptTabNode
from register.register_flow import (
    NodeResult,
    RegisterContext,
    RegisterFlow,
    RegisterFlowRunner,
    RegisterNode,
    Transition,
)


class FakeElement:
    def __init__(self, on_click=None) -> None:
        self.clicks: list[dict[str, object]] = []
        self._on_click = on_click

    async def click(self, *, humanize: bool = False) -> None:
        self.clicks.append({"humanize": humanize})
        if self._on_click is not None:
            self._on_click()


class FakeTab:
    def __init__(
        self,
        current_url: str,
        resolved_url: str | None = None,
        *,
        email_input_initially_available: bool = True,
        login_email_input_available: bool = False,
        signup_button_available: bool = True,
    ) -> None:
        self._current_url = current_url
        self._resolved_url = resolved_url
        self.go_to_urls: list[str] = []
        self.query_calls: list[dict[str, object]] = []
        self.email_input_available = email_input_initially_available
        self.email_input_element = FakeElement()
        self.login_email_input_available = login_email_input_available
        self.login_email_input_element = FakeElement()
        self.signup_button_element = FakeElement(
            on_click=lambda: setattr(self, "email_input_available", True)
        )
        self.signup_button_available = signup_button_available

    async def go_to(self, url: str) -> None:
        self.go_to_urls.append(url)
        self._current_url = self._resolved_url or url

    async def query(self, selector: str, **kwargs) -> FakeElement | None:
        self.query_calls.append({"selector": selector, **kwargs})
        raise_exc = kwargs.get("raise_exc", True)

        if selector == OpenChatGptTabNode.SIGNUP_EMAIL_INPUT_SELECTOR:
            if self.email_input_available:
                return self.email_input_element
            if raise_exc:
                raise AssertionError("邮箱输入框不存在")
            return None

        if selector == OpenChatGptTabNode.LOGIN_EMAIL_INPUT_SELECTOR:
            if self.login_email_input_available:
                return self.login_email_input_element
            if raise_exc:
                raise AssertionError("登录页邮箱输入框不存在")
            return None

        if selector == OpenChatGptTabNode.SIGNUP_BUTTON_SELECTOR:
            if self.signup_button_available:
                return self.signup_button_element
            if raise_exc:
                raise AssertionError("注册按钮不存在")
            return None

        if raise_exc:
            raise AssertionError(f"未知查找条件: {kwargs}")
        return None

    @property
    async def current_url(self) -> str:
        return self._current_url


class FakeBrowser:
    def __init__(
        self,
        current_url: str = "https://chatgpt.com/",
        *,
        email_input_initially_available: bool = True,
        login_email_input_available: bool = False,
        signup_button_available: bool = True,
    ) -> None:
        self.current_url = current_url
        self.started = False
        self.initial_tab = FakeTab(
            "about:blank",
            resolved_url=current_url,
            email_input_initially_available=email_input_initially_available,
            login_email_input_available=login_email_input_available,
            signup_button_available=signup_button_available,
        )

    async def start(self) -> FakeTab:
        self.started = True
        return self.initial_tab


class OpenChatGptTabNodeTest(unittest.TestCase):
    def test_pydoll_initializer_starts_browser_and_stores_initial_tab(self) -> None:
        browser = FakeBrowser()
        initializer = PydollBrowserContextInitializer(browser_factory=lambda: browser)
        ctx = RegisterContext()

        asyncio.run(initializer.initialize(ctx))

        self.assertTrue(browser.started)
        self.assertIs(ctx.get_value("pydoll_browser"), browser)
        self.assertIs(ctx.get_value("pydoll_initial_tab"), browser.initial_tab)
        self.assertIs(ctx.get_value("current_tab"), browser.initial_tab)
        self.assertTrue(ctx.get_value(PYDOLL_BROWSER_CREATED_STATE_KEY))

    def test_execute_creates_browser_and_opens_chatgpt_on_current_tab(self) -> None:
        browser = FakeBrowser()
        node = OpenChatGptTabNode(browser_factory=lambda: browser)
        ctx = RegisterContext()

        result = asyncio.run(node.execute(ctx))
        ctx.update_values(result.data)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "chatgpt_tab_opened")
        self.assertTrue(browser.started)
        self.assertEqual(browser.initial_tab.go_to_urls, ["https://chatgpt.com/"])
        self.assertIs(ctx.get_value("pydoll_browser"), browser)
        self.assertIs(ctx.get_value("pydoll_initial_tab"), browser.initial_tab)
        self.assertIs(ctx.get_value("chatgpt_tab"), browser.initial_tab)
        self.assertIs(ctx.get_value("chatgpt_tab"), ctx.get_value("current_tab"))
        self.assertEqual(ctx.get_value("chatgpt_url"), "https://chatgpt.com/")
        self.assertIs(
            ctx.get_value("chatgpt_email_input"),
            browser.initial_tab.email_input_element,
        )
        self.assertFalse(ctx.get_value("chatgpt_signup_button_clicked"))
        self.assertTrue(ctx.get_value("browser_created"))

    def test_execute_clicks_signup_button_when_dialog_is_missing(self) -> None:
        browser = FakeBrowser(email_input_initially_available=False)
        node = OpenChatGptTabNode(browser_factory=lambda: browser)
        ctx = RegisterContext()

        result = asyncio.run(node.execute(ctx))
        ctx.update_values(result.data)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "chatgpt_tab_opened")
        self.assertEqual(
            browser.initial_tab.signup_button_element.clicks,
            [{"humanize": True}],
        )
        self.assertIs(
            ctx.get_value("chatgpt_email_input"),
            browser.initial_tab.email_input_element,
        )
        self.assertTrue(ctx.get_value("chatgpt_signup_button_clicked"))

    def test_execute_accepts_auth_login_email_input_when_redirected(self) -> None:
        browser = FakeBrowser(
            current_url="https://chatgpt.com/auth/login",
            email_input_initially_available=False,
            login_email_input_available=True,
            signup_button_available=False,
        )
        node = OpenChatGptTabNode(browser_factory=lambda: browser)
        ctx = RegisterContext()

        result = asyncio.run(node.execute(ctx))
        ctx.update_values(result.data)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "chatgpt_tab_opened")
        self.assertEqual(ctx.get_value("chatgpt_url"), "https://chatgpt.com/auth/login")
        self.assertIs(
            ctx.get_value("chatgpt_email_input"),
            browser.initial_tab.login_email_input_element,
        )
        self.assertFalse(ctx.get_value("chatgpt_signup_button_clicked"))

    def test_execute_reuses_existing_browser_from_context(self) -> None:
        browser = FakeBrowser()
        ctx = RegisterContext(
            state={
                "pydoll_browser": browser,
                "pydoll_initial_tab": browser.initial_tab,
                "current_tab": browser.initial_tab,
            }
        )
        node = OpenChatGptTabNode(
            browser_factory=lambda: self.fail("不应该创建新的浏览器")
        )

        result = asyncio.run(node.execute(ctx))

        self.assertTrue(result.success)
        self.assertFalse(browser.started)
        self.assertEqual(browser.initial_tab.go_to_urls, ["https://chatgpt.com/"])
        self.assertFalse(result.data["browser_created"])

    def test_execute_fails_when_opened_url_is_not_expected_domain(self) -> None:
        browser = FakeBrowser(current_url="https://example.com/")
        node = OpenChatGptTabNode(browser_factory=lambda: browser)
        ctx = RegisterContext()

        result = asyncio.run(node.execute(ctx))

        self.assertFalse(result.success)
        self.assertEqual(result.status, "chatgpt_unexpected_url")
        self.assertIn("URL 不符合预期", result.error or "")
        self.assertEqual(result.data["chatgpt_url"], "https://example.com/")

    def test_execute_accepts_custom_target_url(self) -> None:
        browser = FakeBrowser(current_url="https://chatgpt.com/auth/login")
        node = OpenChatGptTabNode(
            target_url="https://chatgpt.com/auth/login",
            browser_factory=lambda: browser,
        )

        result = asyncio.run(node.execute(RegisterContext()))

        self.assertTrue(result.success)
        self.assertEqual(
            browser.initial_tab.go_to_urls,
            ["https://chatgpt.com/auth/login"],
        )
        self.assertEqual(result.data["target_url"], "https://chatgpt.com/auth/login")

    def test_flow_initializer_provides_start_tab_before_open_chatgpt_node(self) -> None:
        browser = FakeBrowser()

        class AssertInitialTabNode(RegisterNode):
            async def execute(self, ctx: RegisterContext) -> NodeResult:
                if ctx.get_value("current_tab") is not browser.initial_tab:
                    return NodeResult.fail(status="missing_initial_tab")
                return NodeResult.ok(status="initial_tab_ready")

        flow = RegisterFlow(
            start_node="assert_initial_tab",
            nodes={
                "assert_initial_tab": AssertInitialTabNode("assert_initial_tab"),
                "open_chatgpt_tab": OpenChatGptTabNode(
                    browser_factory=lambda: self.fail("不应该创建新的浏览器")
                ),
            },
            transitions={
                "assert_initial_tab": [
                    Transition.when_status("initial_tab_ready", "open_chatgpt_tab")
                ],
            },
        )
        ctx = RegisterContext()

        result = asyncio.run(
            RegisterFlowRunner(
                context_initializers=[
                    PydollBrowserContextInitializer(browser_factory=lambda: browser)
                ]
            ).run(flow, ctx)
        )

        self.assertTrue(result.success)
        self.assertTrue(browser.started)
        self.assertEqual(browser.initial_tab.go_to_urls, ["https://chatgpt.com/"])
        self.assertIs(ctx.get_value("pydoll_initial_tab"), browser.initial_tab)
        self.assertIs(ctx.get_value("current_tab"), browser.initial_tab)
        self.assertIs(ctx.get_value("current_tab"), ctx.get_value("chatgpt_tab"))


if __name__ == "__main__":
    unittest.main()
