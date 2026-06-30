from __future__ import annotations

import logging
from urllib.parse import urlparse

from account.account_service import Account
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.fill_email_and_submit_node import FillEmailAndSubmitNode
from register.nodes.wait_email_verification_code_node import WaitEmailVerificationCodeNode
from register.pydoll_clipboard_input import PydollClipboardInput
from register.pydoll_wait import (
    PydollWaitCondition,
    element_exists_condition,
    element_text_contains_condition,
    wait_for_any_condition,
)
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class FillAboutYouNode(RegisterNode):
    """
    填写姓名、年龄并完成 ChatGPT 注册资料页。
    """

    DEFAULT_NAME = "fill_about_you"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.ACCOUNT_STATE_KEY
    NAME_INPUT_STATE_KEY = WaitEmailVerificationCodeNode.ABOUT_YOU_NAME_INPUT_STATE_KEY
    AGE_INPUT_STATE_KEY = WaitEmailVerificationCodeNode.ABOUT_YOU_AGE_INPUT_STATE_KEY
    READY_DIALOG_STATE_KEY = "chatgpt_ready_dialog"
    FINAL_URL_STATE_KEY = "chatgpt_final_url"
    NAME_INPUT_SELECTOR = "input[name='name']"
    AGE_INPUT_SELECTOR = "input[name='age']"
    SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    READY_DIALOG_SELECTOR = "dialog[aria-label='你已准备就绪']"
    CHATGPT_DIALOG_SELECTOR = "dialog"
    CHATGPT_DIALOG_ARIA_LABEL_KEYWORD = "ChatGPT"
    ERROR_MESSAGE_SELECTOR = "ul[class^='_errors_']"
    ACCOUNT_CREATE_ERROR_TEXT = "无法创建你的帐户"
    EXPECTED_CHATGPT_URL = "https://chatgpt.com"
    READY_DIALOG_WAIT_TIMEOUT_SECONDS = 30
    READY_DIALOG_CONDITION = "ready_dialog"
    CHATGPT_DIALOG_CONDITION = "chatgpt_dialog"
    ACCOUNT_CREATE_ERROR_CONDITION = "account_create_error"
    SUCCESS_STATUS = "about_you_submitted"
    FAILED_STATUS = "about_you_submit_failed"
    INVALID_ACCOUNT_STATUS = "account_create_failed"
    UNEXPECTED_URL_STATUS = "chatgpt_unexpected_final_url"

    def __init__(
        self,
        name: str = DEFAULT_NAME,
        *,
        retry_policy: RetryPolicy | None = None,
        expected_chatgpt_url: str = EXPECTED_CHATGPT_URL,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._expected_chatgpt_url = expected_chatgpt_url

    async def execute(self, ctx: RegisterContext) -> NodeResult:
        try:
            return await self._execute_async(ctx)
        except Exception as exc:
            return NodeResult.fail(
                status=self.FAILED_STATUS,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _execute_async(self, ctx: RegisterContext) -> NodeResult:
        tab: Tab = ctx.get_value(self.CURRENT_TAB_STATE_KEY)
        account: Account = ctx.get_value(self.ACCOUNT_STATE_KEY)
        logger.info(
            "准备填写资料页: name=%s %s, age=%s",
            account.first_name,
            account.last_name,
            account.age,
        )

        name_input: WebElement | None = ctx.get_value(self.NAME_INPUT_STATE_KEY)
        if name_input is None:
            logger.info("上下文中没有姓名输入框引用，重新查找")
            name_input = await tab.query(
                self.NAME_INPUT_SELECTOR,
                timeout=10,
                raise_exc=True,
            )

        age_input: WebElement | None = ctx.get_value(self.AGE_INPUT_STATE_KEY)
        if age_input is None:
            logger.info("上下文中没有年龄输入框引用，重新查找")
            age_input = await tab.query(
                self.AGE_INPUT_SELECTOR,
                timeout=10,
                raise_exc=True,
            )

        await PydollClipboardInput.fill_text(
            tab,
            name_input,
            f"{account.first_name} {account.last_name}",
            label="姓名输入框",
        )
        await PydollClipboardInput.fill_text(
            tab,
            age_input,
            str(account.age),
            label="年龄输入框",
        )

        submit_button: WebElement = await tab.query(
            self.SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        logger.info("提交资料页")
        await submit_button.click(humanize=True)

        wait_result = await wait_for_any_condition(
            [
                element_text_contains_condition(
                    self.ACCOUNT_CREATE_ERROR_CONDITION,
                    tab,
                    self.ERROR_MESSAGE_SELECTOR,
                    self.ACCOUNT_CREATE_ERROR_TEXT,
                ),
                element_exists_condition(
                    self.READY_DIALOG_CONDITION,
                    tab,
                    self.READY_DIALOG_SELECTOR,
                ),
                PydollWaitCondition(
                    self.CHATGPT_DIALOG_CONDITION,
                    lambda: _query_chatgpt_dialog(tab, self),
                ),
            ],
            timeout_seconds=self.READY_DIALOG_WAIT_TIMEOUT_SECONDS,
        )
        current_url = await tab.current_url
        logger.info(
            "资料页提交后的页面等待结果: matched=%s, condition=%s, current_url=%s",
            wait_result.matched,
            wait_result.condition_name,
            current_url,
        )
        if not wait_result.matched:
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交资料后未出现准备就绪弹窗，当前 URL: {current_url}",
                data={self.FINAL_URL_STATE_KEY: current_url},
            )

        if wait_result.condition_name == self.ACCOUNT_CREATE_ERROR_CONDITION:
            logger.error("资料页提交后出现账号创建错误")
            return NodeResult.fail(
                status=self.INVALID_ACCOUNT_STATUS,
                error=self.ACCOUNT_CREATE_ERROR_TEXT,
                data={self.FINAL_URL_STATE_KEY: current_url},
            )

        ready_dialog: WebElement = wait_result.value
        if not _is_expected_chatgpt_url(current_url, self._expected_chatgpt_url):
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交资料后 URL 不符合预期: {current_url}",
                data={self.FINAL_URL_STATE_KEY: current_url},
            )

        logger.info("资料页提交完成，ChatGPT 准备就绪弹窗已出现")
        result_data = {
            self.READY_DIALOG_STATE_KEY: ready_dialog,
            self.FINAL_URL_STATE_KEY: current_url,
        }
        return NodeResult.ok(status=self.SUCCESS_STATUS, data=result_data)


def _is_expected_chatgpt_url(current_url: str, expected_url: str) -> bool:
    current = urlparse(current_url)
    expected = urlparse(expected_url)
    return current.scheme == expected.scheme and current.netloc == expected.netloc


async def _query_chatgpt_dialog(
    tab: Tab,
    node: FillAboutYouNode,
) -> WebElement | None:
    dialogs: list[WebElement] = await tab.query(
        node.CHATGPT_DIALOG_SELECTOR,
        find_all=True,
        raise_exc=False,
    ) or []

    for dialog in dialogs:
        aria_label = dialog.get_attribute("aria-label")
        if (
            isinstance(aria_label, str)
            and node.CHATGPT_DIALOG_ARIA_LABEL_KEYWORD in aria_label
        ):
            return dialog
    return None
