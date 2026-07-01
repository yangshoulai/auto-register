from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

from account.account_service import Account
from core.logging_config import mask_email
from email.email_service import EmailAccount
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.pydoll_clipboard_input import PydollClipboardInput
from register.pydoll_wait import (
    PydollWaitCondition,
    element_exists_condition,
    wait_for_any_condition,
)
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class FillEmailAndSubmitNode(RegisterNode):
    """
    创建注册账号和邮箱，填写邮箱地址并提交。

    提交后应该进入邮箱验证码页。
    """

    DEFAULT_NAME = "fill_email_and_submit"
    ACCOUNT_STATE_KEY = "account"
    EMAIL_ACCOUNT_STATE_KEY = "email_account"
    EMAIL_SUBMITTED_AT_STATE_KEY = "email_submitted_at"
    EMAIL_INPUT_STATE_KEY = "chatgpt_email_input"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    VERIFICATION_CODE_INPUT_STATE_KEY = "email_verification_code_input"
    EMAIL_VERIFICATION_URL_STATE_KEY = "email_verification_url"
    EMAIL_INPUT_SELECTOR = "div[role='dialog'] input[id='email']"
    PAGE_EMAIL_INPUT_SELECTOR = "input[name='email']"
    SUBMIT_BUTTON_SELECTOR = "div[role='dialog'] button[type='submit']"
    PAGE_SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    VERIFICATION_CODE_INPUT_SELECTOR = "input[name='code']"
    SMS_VERIFICATION_CODE_INPUT_SELECTOR = "input[name='name']"
    SMS_VERIFICATION_URL_PART = "/phone-verification"
    CREATE_PASSWORD_URL_PART = "/create-account/password"
    PASSWORD_INPUT_SELECTOR = "input[name='new-password']"
    EXPECTED_VERIFICATION_URL = "https://auth.openai.com/email-verification"
    VERIFICATION_CODE_INPUT_WAIT_TIMEOUT_SECONDS = 30
    SUCCESS_STATUS = "email_submitted"
    SMS_VERIFICATION_READY_STATUS = "email_submitted_sms_verification_ready"
    CREATE_PASSWORD_READY_STATUS = "email_submitted_create_password_ready"
    FAILED_STATUS = "email_submit_failed"
    UNEXPECTED_URL_STATUS = "email_verification_unexpected_url"

    def __init__(
            self,
            name: str = DEFAULT_NAME,
            *,
            retry_policy: RetryPolicy | None = None,
            expected_verification_url: str = EXPECTED_VERIFICATION_URL,
            current_tab_state_key: str = CURRENT_TAB_STATE_KEY,
            email_input_state_key: str = EMAIL_INPUT_STATE_KEY,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._expected_verification_url = expected_verification_url
        self._current_tab_state_key = current_tab_state_key
        self._email_input_state_key = email_input_state_key

    async def execute(self, ctx: RegisterContext) -> NodeResult:
        try:
            return await self._execute_async(ctx)
        except Exception as exc:
            return NodeResult.fail(
                status=self.FAILED_STATUS,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _execute_async(self, ctx: RegisterContext) -> NodeResult:
        if ctx.app_context is None:
            raise RuntimeError("注册上下文缺少 AppContext，无法创建账号和邮箱")

        tab: Tab = ctx.get_value(self._current_tab_state_key)
        account = ctx.app_context.account_service.create_account()
        logger.info(
            "账号基础信息已生成: name=%s %s, age=%s",
            account.first_name,
            account.last_name,
            account.age,
        )
        email_account = ctx.app_context.email_service.generate_email_address()
        account.email_address = email_account.email_address
        logger.info(
            "邮箱地址已生成: email=%s",
            mask_email(email_account.email_address),
        )

        email_input: WebElement | None = ctx.get_value(self._email_input_state_key)
        if email_input is None:
            logger.info("上下文中没有邮箱输入框引用，重新查找输入框")
            email_input = await self._query_email_input(tab)

        logger.info("填写邮箱地址并提交")
        await PydollClipboardInput.fill_text(
            tab,
            email_input,
            email_account.email_address,
            label="邮箱地址输入框",
        )

        submit_button = await self._query_submit_button(tab)
        email_submitted_at = datetime.now(UTC)
        await submit_button.click(humanize=True)

        wait_result = await wait_for_any_condition(
            [
                element_exists_condition(
                    "verification_code_input",
                    tab,
                    self.VERIFICATION_CODE_INPUT_SELECTOR,
                ),
                PydollWaitCondition(
                    "sms_verification_input",
                    lambda: _check_sms_verification_ready(tab, self),
                ),
                PydollWaitCondition(
                    "create_password_input",
                    lambda: _check_create_password_ready(tab, self),
                ),
            ],
            timeout_seconds=self.VERIFICATION_CODE_INPUT_WAIT_TIMEOUT_SECONDS,
        )
        current_url = await tab.current_url
        logger.debug(
            "邮箱提交后的页面等待结果: matched=%s, condition=%s, current_url=%s",
            wait_result.matched,
            wait_result.condition_name,
            current_url,
        )
        if not wait_result.matched:
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交邮箱后未进入验证码页面: {current_url}",
                data={
                    self.ACCOUNT_STATE_KEY: account,
                    self.EMAIL_ACCOUNT_STATE_KEY: email_account,
                    self.EMAIL_SUBMITTED_AT_STATE_KEY: email_submitted_at,
                    self.EMAIL_VERIFICATION_URL_STATE_KEY: current_url,
                },
            )

        if wait_result.condition_name == "sms_verification_input":
            logger.info("邮箱提交后直接进入手机号验证页面")
            return NodeResult.ok(
                status=self.SMS_VERIFICATION_READY_STATUS,
                data={
                    self.ACCOUNT_STATE_KEY: account,
                    self.EMAIL_ACCOUNT_STATE_KEY: email_account,
                    self.EMAIL_SUBMITTED_AT_STATE_KEY: email_submitted_at,
                    self.EMAIL_VERIFICATION_URL_STATE_KEY: current_url,
                    "phone_submitted_at": email_submitted_at,
                },
            )

        if wait_result.condition_name == "create_password_input":
            logger.info("邮箱提交后进入创建密码页面")
            return NodeResult.ok(
                status=self.CREATE_PASSWORD_READY_STATUS,
                data={
                    self.ACCOUNT_STATE_KEY: account,
                    self.EMAIL_ACCOUNT_STATE_KEY: email_account,
                    self.EMAIL_SUBMITTED_AT_STATE_KEY: email_submitted_at,
                    self.EMAIL_VERIFICATION_URL_STATE_KEY: current_url,
                },
            )

        verification_code_input: WebElement = wait_result.value
        result_data = _create_result_data(
            account=account,
            email_account=email_account,
            email_submitted_at=email_submitted_at,
            verification_code_input=verification_code_input,
            current_url=current_url,
        )

        if not _is_expected_url(current_url, self._expected_verification_url):
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交邮箱后 URL 不符合预期: {current_url}",
                data=result_data,
            )

        logger.info("邮箱验证码页面已就绪")
        return NodeResult.ok(status=self.SUCCESS_STATUS, data=result_data)

    async def _query_email_input(self, tab: Tab) -> WebElement:
        logger.info("查找注册弹窗邮箱输入框: selector=%s", self.EMAIL_INPUT_SELECTOR)
        email_input: WebElement | None = await tab.query(
            self.EMAIL_INPUT_SELECTOR,
            timeout=5,
            raise_exc=False,
        )
        if email_input is not None:
            logger.info("找到注册弹窗邮箱输入框")
            return email_input

        logger.info("查找页面邮箱输入框: selector=%s", self.PAGE_EMAIL_INPUT_SELECTOR)
        return await tab.query(
            self.PAGE_EMAIL_INPUT_SELECTOR,
            timeout=10,
            raise_exc=True,
        )

    async def _query_submit_button(self, tab: Tab) -> WebElement:
        logger.debug("查找注册弹窗提交按钮: selector=%s", self.SUBMIT_BUTTON_SELECTOR)
        submit_button: WebElement | None = await tab.query(
            self.SUBMIT_BUTTON_SELECTOR,
            timeout=5,
            raise_exc=False,
        )
        if submit_button is not None:
            logger.debug("找到注册弹窗提交按钮")
            return submit_button

        logger.info("查找页面提交按钮: selector=%s", self.PAGE_SUBMIT_BUTTON_SELECTOR)
        return await tab.query(
            self.PAGE_SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )


def _create_result_data(
        *,
        account: Account,
        email_account: EmailAccount,
        email_submitted_at: datetime,
        verification_code_input: WebElement,
        current_url: str,
) -> dict[str, object]:
    return {
        FillEmailAndSubmitNode.ACCOUNT_STATE_KEY: account,
        FillEmailAndSubmitNode.EMAIL_ACCOUNT_STATE_KEY: email_account,
        FillEmailAndSubmitNode.EMAIL_SUBMITTED_AT_STATE_KEY: email_submitted_at,
        FillEmailAndSubmitNode.VERIFICATION_CODE_INPUT_STATE_KEY: verification_code_input,
        FillEmailAndSubmitNode.EMAIL_VERIFICATION_URL_STATE_KEY: current_url,
    }


async def _check_sms_verification_ready(
        tab: Tab,
        node: FillEmailAndSubmitNode,
) -> WebElement | None:
    current_url = await tab.current_url
    if node.SMS_VERIFICATION_URL_PART not in current_url:
        return None

    return await tab.query(node.SMS_VERIFICATION_CODE_INPUT_SELECTOR, raise_exc=False)


async def _check_create_password_ready(
        tab: Tab,
        node: FillEmailAndSubmitNode,
) -> WebElement | None:
    current_url = await tab.current_url
    if node.CREATE_PASSWORD_URL_PART not in current_url:
        return None

    return await tab.query(node.PASSWORD_INPUT_SELECTOR, raise_exc=False)


def _is_expected_url(current_url: str, expected_url: str) -> bool:
    current = urlparse(current_url)
    expected = urlparse(expected_url)
    return (
            current.scheme == expected.scheme
            and current.netloc == expected.netloc
            and current.path.rstrip("/") == expected.path.rstrip("/")
    )
