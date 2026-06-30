from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from account.account_service import Account
from account_export.account_export_service import AccountExportOauthUrl
from core.logging_config import mask_email, sanitize_url
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.fill_email_and_submit_node import FillEmailAndSubmitNode
from register.pydoll_clipboard_input import PydollClipboardInput
from register.pydoll_wait import PydollWaitCondition, wait_for_any_condition
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class SelectCodexAccountNode(RegisterNode):
    """
    打开 Codex OAuth 链接，并在 OpenAI 账号选择页选择当前注册邮箱。
    """

    DEFAULT_NAME = "select_codex_account"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.ACCOUNT_STATE_KEY
    OAUTH_URL_STATE_KEY = "codex_oauth_url"
    SELECTED_ACCOUNT_BUTTON_STATE_KEY = "codex_selected_account_button"
    NEXT_URL_STATE_KEY = "codex_oauth_next_url"
    CHOOSE_ACCOUNT_URL = "https://auth.openai.com/choose-an-account"
    CHOOSE_ACCOUNT_BUTTON_SELECTOR = "button"
    EMAIL_SPAN_SELECTOR = "span"
    EMAIL_INPUT_SELECTOR = "input[name='email']"
    EMAIL_SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    ADD_PHONE_URL_PART = "/add-phone"
    CONSENT_URL_PART = "/sign-in-with-chatgpt/codex/consent"
    CHOOSE_ACCOUNT_WAIT_TIMEOUT_SECONDS = 30
    NEXT_PAGE_WAIT_TIMEOUT_SECONDS = 30
    SUCCESS_NEEDS_PHONE_STATUS = "codex_oauth_needs_phone"
    SUCCESS_CONSENT_STATUS = "codex_oauth_consent_ready"
    SUCCESS_EMAIL_VERIFICATION_READY_STATUS = "codex_oauth_email_verification_ready"
    FAILED_STATUS = "codex_oauth_account_select_failed"
    UNEXPECTED_URL_STATUS = "codex_oauth_unexpected_url"

    def __init__(
        self,
        name: str = DEFAULT_NAME,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)

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
            raise RuntimeError("注册上下文缺少 AppContext，无法获取 Codex OAuth 链接")

        tab: Tab = ctx.get_value(self.CURRENT_TAB_STATE_KEY)
        account: Account = ctx.get_value(self.ACCOUNT_STATE_KEY)
        if account.email_address is None:
            raise RuntimeError("账号缺少邮箱地址，无法选择 OAuth 账号")

        logger.info("获取 Codex OAuth 授权链接")
        oauth_url = ctx.app_context.account_export_service.get_oauth_url()
        logger.info("访问 Codex OAuth 授权链接: url=%s", sanitize_url(oauth_url.url))
        await tab.go_to(oauth_url.url)

        logger.info(
            "等待账号选择按钮: email=%s",
            mask_email(account.email_address),
        )
        button_result = await wait_for_any_condition(
            [
                PydollWaitCondition(
                    "account_button",
                    lambda: _find_account_button(tab, account.email_address or ""),
                ),
                PydollWaitCondition(
                    "email_input",
                    lambda: _query_element(tab, self.EMAIL_INPUT_SELECTOR),
                ),
            ],
            timeout_seconds=self.CHOOSE_ACCOUNT_WAIT_TIMEOUT_SECONDS,
        )
        current_url = await tab.current_url
        logger.info(
            "账号选择页等待结果: matched=%s, current_url=%s",
            button_result.matched,
            current_url,
        )
        if not button_result.matched:
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"未找到账号选择按钮，当前 URL: {current_url}",
                data=_create_result_data(oauth_url=oauth_url, current_url=current_url),
            )

        if button_result.condition_name == "email_input":
            email_input: WebElement = button_result.value
            return await self._submit_email_login(
                tab=tab,
                account=account,
                oauth_url=oauth_url,
                email_input=email_input,
            )

        account_button: WebElement = button_result.value
        logger.info("点击 OAuth 账号按钮: email=%s", mask_email(account.email_address))
        await account_button.click(humanize=True)

        next_page_result = await wait_for_any_condition(
            [
                PydollWaitCondition(
                    self.SUCCESS_NEEDS_PHONE_STATUS,
                    lambda: _current_url_contains(tab, self.ADD_PHONE_URL_PART),
                ),
                PydollWaitCondition(
                    self.SUCCESS_CONSENT_STATUS,
                    lambda: _current_url_contains(tab, self.CONSENT_URL_PART),
                ),
            ],
            timeout_seconds=self.NEXT_PAGE_WAIT_TIMEOUT_SECONDS,
        )
        next_url = await tab.current_url
        logger.info(
            "OAuth 账号选择后的页面结果: matched=%s, condition=%s, next_url=%s",
            next_page_result.matched,
            next_page_result.condition_name,
            next_url,
        )
        result_data = _create_result_data(
            oauth_url=oauth_url,
            current_url=next_url,
            account_button=account_button,
        )
        if not next_page_result.matched:
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"选择账号后未进入手机号或 consent 页面: {next_url}",
                data=result_data,
            )

        logger.info("OAuth 下一步页面已就绪: status=%s", next_page_result.condition_name)
        return NodeResult.ok(
            status=next_page_result.condition_name or "",
            data=result_data,
        )

    async def _submit_email_login(
        self,
        *,
        tab: Tab,
        account: Account,
        oauth_url: AccountExportOauthUrl,
        email_input: WebElement,
    ) -> NodeResult:
        if account.email_address is None:
            raise RuntimeError("账号缺少邮箱地址，无法提交 OAuth 邮箱登录")

        logger.info(
            "OAuth 页面要求重新登录，填写邮箱地址: email=%s",
            mask_email(account.email_address),
        )
        await PydollClipboardInput.fill_text(
            tab,
            email_input,
            account.email_address,
            label="Codex OAuth 邮箱输入框",
        )

        submit_button: WebElement = await tab.query(
            self.EMAIL_SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        email_submitted_at = datetime.now(UTC)
        logger.info("提交 Codex OAuth 邮箱登录表单")
        await submit_button.click(humanize=True)

        current_url = await tab.current_url
        result_data = _create_result_data(
            oauth_url=oauth_url,
            current_url=current_url,
            email_submitted_at=email_submitted_at,
        )
        logger.info(
            "Codex OAuth 邮箱登录已提交，流转到邮箱验证码节点: current_url=%s",
            current_url,
        )
        return NodeResult.ok(
            status=self.SUCCESS_EMAIL_VERIFICATION_READY_STATUS,
            data=result_data,
        )


@dataclass(frozen=True)
class AccountButtonMatch:
    button: WebElement
    text: str


async def _find_account_button(tab: Tab, email_address: str) -> WebElement | None:
    buttons = await _query_all(tab, SelectCodexAccountNode.CHOOSE_ACCOUNT_BUTTON_SELECTOR)
    for button in buttons:
        match = await _match_account_button(button, email_address)
        if match is not None:
            return match.button
    return None


async def _match_account_button(
    button: WebElement,
    email_address: str,
) -> AccountButtonMatch | None:
    spans = await _query_all(button, SelectCodexAccountNode.EMAIL_SPAN_SELECTOR)
    for span in spans:
        text = await span.text
        if email_address in text:
            return AccountButtonMatch(button=button, text=text)

    button_text = await button.text
    if email_address in button_text:
        return AccountButtonMatch(button=button, text=button_text)
    return None


async def _query_all(target: Tab | WebElement, selector: str) -> list[WebElement]:
    elements = await target.query(selector, find_all=True, raise_exc=False)
    if elements is None:
        return []
    if isinstance(elements, list):
        return elements
    return [elements]


async def _query_element(tab: Tab, selector: str) -> WebElement | None:
    return await tab.query(selector, raise_exc=False)


async def _current_url_contains(tab: Tab, url_part: str) -> str | None:
    current_url = await tab.current_url
    if url_part in current_url:
        return current_url
    return None


def _create_result_data(
    *,
    oauth_url: AccountExportOauthUrl,
    current_url: str,
    account_button: WebElement | None = None,
    email_submitted_at: datetime | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        SelectCodexAccountNode.OAUTH_URL_STATE_KEY: oauth_url,
        SelectCodexAccountNode.NEXT_URL_STATE_KEY: current_url,
    }
    if account_button is not None:
        result[SelectCodexAccountNode.SELECTED_ACCOUNT_BUTTON_STATE_KEY] = account_button
    if email_submitted_at is not None:
        result[FillEmailAndSubmitNode.EMAIL_SUBMITTED_AT_STATE_KEY] = email_submitted_at
    return result
