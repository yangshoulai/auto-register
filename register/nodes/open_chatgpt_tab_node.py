from __future__ import annotations

import logging
from urllib.parse import urlparse

from pydoll.browser.chromium.base import Browser
from pydoll.browser import Chrome
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from register.browser_context import (
    BrowserFactory,
    CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY,
    PYDOLL_BROWSER_STATE_KEY as DEFAULT_BROWSER_STATE_KEY,
    PYDOLL_INITIAL_TAB_STATE_KEY as DEFAULT_INITIAL_TAB_STATE_KEY,
)
from register.register_flow import (
    NodeResult,
    RegisterContext,
    RegisterNode,
    RetryPolicy,
)

logger = logging.getLogger(__name__)


class OpenChatGptTabNode(RegisterNode):
    """
    使用 pydoll 在当前 TAB 打开 ChatGPT。
    """

    DEFAULT_NAME = "open_chatgpt_tab"
    DEFAULT_TARGET_URL = "https://chatgpt.com/"
    BROWSER_STATE_KEY = DEFAULT_BROWSER_STATE_KEY
    INITIAL_TAB_STATE_KEY = DEFAULT_INITIAL_TAB_STATE_KEY
    TAB_STATE_KEY = "chatgpt_tab"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    URL_STATE_KEY = "chatgpt_url"
    EMAIL_INPUT_STATE_KEY = "chatgpt_email_input"
    SIGNUP_BUTTON_CLICKED_STATE_KEY = "chatgpt_signup_button_clicked"
    SUCCESS_STATUS = "chatgpt_tab_opened"
    FAILED_STATUS = "chatgpt_tab_open_failed"
    UNEXPECTED_URL_STATUS = "chatgpt_unexpected_url"
    SIGNUP_EMAIL_INPUT_SELECTOR = "div[role='dialog'] input[id='email']"
    LOGIN_EMAIL_INPUT_SELECTOR = "input[name='email']"
    SIGNUP_BUTTON_SELECTOR = "button[data-testid='signup-button']"

    def __init__(
            self,
            name: str = DEFAULT_NAME,
            *,
            target_url: str = DEFAULT_TARGET_URL,
            browser_state_key: str = BROWSER_STATE_KEY,
            initial_tab_state_key: str = INITIAL_TAB_STATE_KEY,
            tab_state_key: str = TAB_STATE_KEY,
            current_tab_state_key: str = CURRENT_TAB_STATE_KEY,
            retry_policy: RetryPolicy | None = None,
            browser_factory: BrowserFactory | None = None,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._target_url = target_url
        self._browser_state_key = browser_state_key
        self._initial_tab_state_key = initial_tab_state_key
        self._tab_state_key = tab_state_key
        self._current_tab_state_key = current_tab_state_key
        self._browser_factory = browser_factory or _create_default_browser
        self._expected_netloc = urlparse(target_url).netloc

    async def execute(self, ctx: RegisterContext) -> NodeResult:
        try:
            return await self._execute_async(ctx)
        except Exception as exc:
            return NodeResult.fail(
                status=self.FAILED_STATUS,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _execute_async(self, ctx: RegisterContext) -> NodeResult:
        browser: Browser | None = ctx.get_value(self._browser_state_key)
        browser_created = False
        if browser is None:
            logger.info("未发现浏览器上下文，创建并启动新浏览器")
            browser = self._browser_factory()
            initial_tab = await browser.start()
            browser_created = True
        else:
            logger.debug("使用上下文中的浏览器打开 ChatGPT")
            initial_tab: Tab | None = ctx.get_value(self._initial_tab_state_key)
            if initial_tab is None:
                initial_tab = ctx.get_value(self._current_tab_state_key)
            if initial_tab is None:
                logger.info("上下文中没有可用 TAB，重新启动浏览器 TAB")
                initial_tab = await browser.start()

        tab = initial_tab
        logger.info("访问 ChatGPT: %s", self._target_url)
        await _navigate_tab(tab, self._target_url)
        current_url = await _read_tab_current_url(tab)
        if not _is_expected_url(current_url, self._expected_netloc):
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"打开 TAB 后 URL 不符合预期: {current_url}",
                data={
                    self._browser_state_key: browser,
                    self._initial_tab_state_key: initial_tab,
                    self._tab_state_key: tab,
                    self._current_tab_state_key: tab,
                    self.URL_STATE_KEY: current_url,
                    "browser_created": browser_created,
                    "target_url": self._target_url,
                },
            )

        email_input, signup_button_clicked = await _ensure_signup_dialog_email_input(
            tab,
            email_input_selector=self.SIGNUP_EMAIL_INPUT_SELECTOR,
            login_email_input_selector=self.LOGIN_EMAIL_INPUT_SELECTOR,
            signup_button_selector=self.SIGNUP_BUTTON_SELECTOR,
        )
        logger.debug(
            "邮箱输入入口已就绪: signup_button_clicked=%s",
            signup_button_clicked,
        )

        return NodeResult.ok(
            status=self.SUCCESS_STATUS,
            data={
                self._browser_state_key: browser,
                self._initial_tab_state_key: initial_tab,
                self._tab_state_key: tab,
                self._current_tab_state_key: tab,
                self.URL_STATE_KEY: current_url,
                self.EMAIL_INPUT_STATE_KEY: email_input,
                self.SIGNUP_BUTTON_CLICKED_STATE_KEY: signup_button_clicked,
                "browser_created": browser_created,
                "target_url": self._target_url,
            },
        )


def _create_default_browser() -> Chrome:
    return Chrome()


async def _ensure_signup_dialog_email_input(
        tab: Tab,
        *,
        email_input_selector: str,
        login_email_input_selector: str,
        signup_button_selector: str,
) -> tuple[WebElement, bool]:
    logger.debug("查找注册弹窗邮箱输入框: selector=%s", email_input_selector)
    email_input = await tab.query(email_input_selector, timeout=2, raise_exc=False)
    if email_input is not None:
        logger.debug("找到注册弹窗邮箱输入框")
        return email_input, False

    logger.debug("注册弹窗未出现，检查登录页邮箱输入框: selector=%s", login_email_input_selector)
    login_email_input = await tab.query(
        login_email_input_selector,
        timeout=5,
        raise_exc=False,
    )
    if login_email_input is not None:
        logger.info("找到登录页邮箱输入框")
        return login_email_input, False

    logger.info("点击注册按钮")
    signup_button = await tab.query(signup_button_selector, timeout=10, raise_exc=True)
    await signup_button.click(humanize=True)

    email_input = await tab.query(email_input_selector, timeout=10, raise_exc=True)
    logger.debug("注册弹窗邮箱输入框已出现")
    return email_input, True


async def _navigate_tab(tab: Tab, target_url: str) -> None:
    await tab.go_to(target_url)


async def _read_tab_current_url(tab: Tab) -> str:
    return str(await tab.current_url)


def _is_expected_url(current_url: str, expected_netloc: str) -> bool:
    parsed_url = urlparse(current_url)
    return (
            parsed_url.scheme in {"http", "https"}
            and parsed_url.netloc == expected_netloc
    )
