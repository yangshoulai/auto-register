from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from account.account_service import Account
from email.email_service import EmailAccount
from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.fill_email_and_submit_node import FillEmailAndSubmitNode
from register.pydoll_clipboard_input import PydollClipboardInput
from register.pydoll_wait import element_exists_condition, wait_for_any_condition
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class CreatePasswordNode(RegisterNode):
    """
    创建账号初始密码，并等待进入邮箱验证码页面。
    """

    DEFAULT_NAME = "create_password"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.ACCOUNT_STATE_KEY
    EMAIL_ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.EMAIL_ACCOUNT_STATE_KEY
    EMAIL_SUBMITTED_AT_STATE_KEY = FillEmailAndSubmitNode.EMAIL_SUBMITTED_AT_STATE_KEY
    VERIFICATION_CODE_INPUT_STATE_KEY = FillEmailAndSubmitNode.VERIFICATION_CODE_INPUT_STATE_KEY
    EMAIL_VERIFICATION_URL_STATE_KEY = FillEmailAndSubmitNode.EMAIL_VERIFICATION_URL_STATE_KEY
    PASSWORD_INPUT_SELECTOR = "input[name='new-password']"
    SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    VERIFICATION_CODE_INPUT_SELECTOR = "input[name='code']"
    CREATE_PASSWORD_URL_PART = "/create-account/password"
    EXPECTED_VERIFICATION_URL = "https://auth.openai.com/email-verification"
    VERIFICATION_CODE_INPUT_WAIT_TIMEOUT_SECONDS = 30
    SUCCESS_STATUS = "password_created"
    FAILED_STATUS = "password_create_failed"
    UNEXPECTED_URL_STATUS = "password_create_unexpected_url"

    def __init__(
            self,
            name: str = DEFAULT_NAME,
            *,
            retry_policy: RetryPolicy | None = None,
            expected_verification_url: str = EXPECTED_VERIFICATION_URL,
            current_tab_state_key: str = CURRENT_TAB_STATE_KEY,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._expected_verification_url = expected_verification_url
        self._current_tab_state_key = current_tab_state_key

    async def execute(self, ctx: RegisterContext) -> NodeResult:
        try:
            return await self._execute_async(ctx)
        except Exception as exc:
            return NodeResult.fail(
                status=self.FAILED_STATUS,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _execute_async(self, ctx: RegisterContext) -> NodeResult:
        tab: Tab = ctx.get_value(self._current_tab_state_key)
        account: Account = ctx.get_value(self.ACCOUNT_STATE_KEY)
        email_account: EmailAccount = ctx.get_value(self.EMAIL_ACCOUNT_STATE_KEY)

        if account is None:
            raise RuntimeError("注册上下文缺少账号信息，无法填写密码")
        if email_account is None:
            raise RuntimeError("注册上下文缺少邮箱信息，无法确认密码页邮箱")
        if not account.password:
            raise RuntimeError("账号密码为空，无法填写密码")

        logger.info("等待创建密码页面就绪: email=%s", email_account.email_address)
        password_input = await self._query_password_input(tab)
        current_url = await tab.current_url
        if self.CREATE_PASSWORD_URL_PART not in current_url:
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"当前页面不是创建密码页: {current_url}",
                data={
                    self.ACCOUNT_STATE_KEY: account,
                    self.EMAIL_ACCOUNT_STATE_KEY: email_account,
                    self.EMAIL_VERIFICATION_URL_STATE_KEY: current_url,
                },
            )
        await self._ensure_email_input_value(tab, email_account.email_address)

        logger.info("填写账号初始密码")
        await password_input.type_text(account.password, True)

        submit_button: WebElement = await tab.query(
            self.SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        email_submitted_at = datetime.now(UTC)
        await submit_button.click(humanize=True)

        wait_result = await wait_for_any_condition(
            [
                element_exists_condition(
                    "verification_code_input",
                    tab,
                    self.VERIFICATION_CODE_INPUT_SELECTOR,
                )
            ],
            timeout_seconds=self.VERIFICATION_CODE_INPUT_WAIT_TIMEOUT_SECONDS,
        )
        current_url = await tab.current_url
        logger.info(
            "密码提交后的页面等待结果: matched=%s, condition=%s, current_url=%s",
            wait_result.matched,
            wait_result.condition_name,
            current_url,
        )
        result_data = _create_result_data(
            account=account,
            email_account=email_account,
            email_submitted_at=email_submitted_at,
            verification_code_input=wait_result.value if wait_result.matched else None,
            current_url=current_url,
        )
        if not wait_result.matched:
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交密码后未进入邮箱验证码页面: {current_url}",
                data=result_data,
            )

        if not _is_expected_url(current_url, self._expected_verification_url):
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交密码后 URL 不符合预期: {current_url}",
                data=result_data,
            )

        logger.info("密码创建完成，邮箱验证码页面已就绪")
        return NodeResult.ok(status=self.SUCCESS_STATUS, data=result_data)

    async def _query_password_input(self, tab: Tab) -> WebElement:
        return await tab.query(
            self.PASSWORD_INPUT_SELECTOR,
            timeout=10,
            raise_exc=True,
        )

    async def _ensure_email_input_value(self, tab: Tab, email_address: str) -> None:
        email_input = await _query_input_by_value(tab, email_address)
        if email_input is not None:
            logger.info("创建密码页邮箱输入框已带默认邮箱值")
            return

        raise RuntimeError(f"创建密码页邮箱输入框的默认值不是当前邮箱: {email_address}")


def _create_result_data(
        *,
        account: Account,
        email_account: EmailAccount,
        email_submitted_at: datetime,
        verification_code_input: WebElement | None,
        current_url: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        CreatePasswordNode.ACCOUNT_STATE_KEY: account,
        CreatePasswordNode.EMAIL_ACCOUNT_STATE_KEY: email_account,
        CreatePasswordNode.EMAIL_SUBMITTED_AT_STATE_KEY: email_submitted_at,
        CreatePasswordNode.EMAIL_VERIFICATION_URL_STATE_KEY: current_url,
    }
    if verification_code_input is not None:
        result[CreatePasswordNode.VERIFICATION_CODE_INPUT_STATE_KEY] = (
            verification_code_input
        )
    return result


async def _query_input_by_value(tab: Tab, expected_value: str) -> WebElement | None:
    query_result = await tab.query(
        "input",
        find_all=True,
        raise_exc=False,
    )
    if query_result is None:
        return None
    inputs = query_result if isinstance(query_result, list) else [query_result]
    for input_element in inputs:
        current_value = await PydollClipboardInput.read_value(input_element)
        if current_value == expected_value:
            return input_element
    return None


def _is_expected_url(current_url: str, expected_url: str) -> bool:
    current = urlparse(current_url)
    expected = urlparse(expected_url)
    return (
            current.scheme == expected.scheme
            and current.netloc == expected.netloc
            and current.path.rstrip("/") == expected.path.rstrip("/")
    )
