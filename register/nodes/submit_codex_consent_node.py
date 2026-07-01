from __future__ import annotations

import logging

from account_export.account_export_service import AccountExportSubmitResult
from core.logging_config import mask_email, sanitize_url
from email.email_service import EmailAccount
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.fill_email_and_submit_node import FillEmailAndSubmitNode
from register.pydoll_wait import PydollWaitCondition, wait_for_any_condition
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class SubmitCodexConsentNode(RegisterNode):
    """
    提交 Codex consent，并把 localhost 回调地址提交给账号导出服务。
    """

    DEFAULT_NAME = "submit_codex_consent"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    EMAIL_ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.EMAIL_ACCOUNT_STATE_KEY
    REDIRECT_URL_STATE_KEY = "codex_oauth_redirect_url"
    ACCOUNT_EXPORT_SUBMIT_RESULT_STATE_KEY = "account_export_submit_result"
    SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    FALLBACK_BUTTON_SELECTOR = "button"
    LOCALHOST_REDIRECT_PREFIX = "http://localhost"
    LOCALHOST_REDIRECT_WAIT_TIMEOUT_SECONDS = 30
    SUCCESS_STATUS = "codex_account_exported"
    FAILED_STATUS = "codex_consent_submit_failed"
    EXPORT_FAILED_STATUS = "account_export_failed"
    REDIRECT_TIMEOUT_STATUS = "codex_oauth_redirect_timeout"

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
            raise RuntimeError("注册上下文缺少 AppContext，无法提交 Codex consent")

        tab: Tab = ctx.get_value(self.CURRENT_TAB_STATE_KEY)
        email_account: EmailAccount = ctx.get_value(self.EMAIL_ACCOUNT_STATE_KEY)
        logger.debug("准备提交 Codex consent: email=%s", mask_email(email_account.email_address))
        submit_button = await self._query_submit_button(tab)
        logger.info("点击 Codex consent 提交按钮")
        await submit_button.click(humanize=True)

        logger.info("等待 OAuth localhost 回调地址")
        wait_result = await wait_for_any_condition(
            [
                PydollWaitCondition(
                    "localhost_redirect",
                    lambda: _current_url_startswith(tab, self.LOCALHOST_REDIRECT_PREFIX),
                )
            ],
            timeout_seconds=self.LOCALHOST_REDIRECT_WAIT_TIMEOUT_SECONDS,
        )
        current_url = await tab.current_url
        logger.info(
            "Codex consent 提交后的回调等待结果: matched=%s, current_url=%s",
            wait_result.matched,
            sanitize_url(current_url),
        )
        if not wait_result.matched:
            return NodeResult.fail(
                status=self.REDIRECT_TIMEOUT_STATUS,
                error=f"提交 consent 后未跳转到 localhost 回调地址: {current_url}",
                data={self.REDIRECT_URL_STATE_KEY: current_url},
            )

        redirect_url: str = wait_result.value
        logger.info("提交 OAuth redirect_url 到账号导出服务: redirect_url=%s", sanitize_url(redirect_url))
        submit_result = ctx.app_context.account_export_service.submit_redirect_url(
            redirect_url
        )
        result_data = _create_result_data(
            redirect_url=redirect_url,
            submit_result=submit_result,
        )
        if not submit_result.success:
            logger.error(
                "账号导出服务提交失败: status=%s, error=%s",
                submit_result.status,
                submit_result.error or "",
            )
            return NodeResult.fail(
                status=self.EXPORT_FAILED_STATUS,
                error=submit_result.error or f"账号导出失败: {submit_result.status}",
                data=result_data,
            )

        logger.debug("账号导出成功，执行邮箱服务回调")
        ctx.app_context.email_service.callback(email_account, is_email_used=True)
        return NodeResult.ok(status=self.SUCCESS_STATUS, data=result_data)

    async def _query_submit_button(self, tab: Tab) -> WebElement:
        submit_button: WebElement | None = await tab.query(
            self.SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=False,
        )
        if submit_button is not None:
            return submit_button

        return await tab.query(
            self.FALLBACK_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )


async def _current_url_startswith(tab: Tab, prefix: str) -> str | None:
    current_url = await tab.current_url
    if current_url.startswith(prefix):
        return current_url
    return None


def _create_result_data(
    *,
    redirect_url: str,
    submit_result: AccountExportSubmitResult,
) -> dict[str, object]:
    return {
        SubmitCodexConsentNode.REDIRECT_URL_STATE_KEY: redirect_url,
        SubmitCodexConsentNode.ACCOUNT_EXPORT_SUBMIT_RESULT_STATE_KEY: submit_result,
    }
