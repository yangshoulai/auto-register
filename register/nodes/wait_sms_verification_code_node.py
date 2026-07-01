from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from core.logging_config import mask_phone
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement
from sms.sms_service import SmsMobileNumber

from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.add_phone_number_node import AddPhoneNumberNode
from register.pydoll_clipboard_input import PydollClipboardInput, PydollClipboardInputError
from register.pydoll_wait import (
    PydollWaitCondition,
    PydollWaitResult,
    wait_for_any_condition,
)
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class WaitSmsVerificationCodeNode(RegisterNode):
    """
    等待短信验证码，填写提交，并进入 Codex consent 页面。
    """

    DEFAULT_NAME = "wait_sms_verification_code"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    SMS_MOBILE_NUMBER_STATE_KEY = AddPhoneNumberNode.SMS_MOBILE_NUMBER_STATE_KEY
    PHONE_SUBMITTED_AT_STATE_KEY = AddPhoneNumberNode.PHONE_SUBMITTED_AT_STATE_KEY
    SMS_VERIFICATION_CODE_STATE_KEY = "sms_verification_code"
    SMS_VERIFICATION_RETRY_COUNT_STATE_KEY = "sms_verification_retry_count"
    CONSENT_URL_STATE_KEY = "codex_consent_url"
    MAX_RESEND_ATTEMPTS = 1
    CODE_INPUT_SELECTOR = "input[name='code']"
    RESEND_BUTTON_SELECTOR = "button[value='resend']"
    SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    ERROR_MESSAGE_SELECTOR = "ul[class^='_errors_']"
    CONSENT_URL_PART = "/sign-in-with-chatgpt/codex/consent"
    CODE_FILL_MAX_ATTEMPTS = 3
    SUBMIT_RESULT_WAIT_TIMEOUT_SECONDS = 30
    SUCCESS_STATUS = "phone_verified"
    RETRY_SELECT_CODEX_ACCOUNT_STATUS = "sms_verification_retry_select_codex_account"
    FAILED_STATUS = "phone_verification_failed"
    TIMEOUT_STATUS = "sms_verification_code_timeout"
    ERROR_STATUS = "sms_verification_error"
    CODE_FILL_MISMATCH_STATUS = "sms_verification_code_fill_mismatch"
    UNEXPECTED_URL_STATUS = "codex_consent_unexpected_url"

    def __init__(
            self,
            name: str = DEFAULT_NAME,
            *,
            retry_policy: RetryPolicy | None = None,
            now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._now = now or (lambda: datetime.now(UTC))

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
            raise RuntimeError("注册上下文缺少 AppContext，无法查询短信验证码")
        if ctx.app_context.sms_service is None:
            raise RuntimeError("未配置短信服务，无法查询短信验证码")

        tab: Tab = ctx.get_value(self.CURRENT_TAB_STATE_KEY)
        mobile_number: SmsMobileNumber = ctx.get_value(self.SMS_MOBILE_NUMBER_STATE_KEY)
        sent_after: datetime = ctx.get_value(self.PHONE_SUBMITTED_AT_STATE_KEY)
        resend_attempts = 0

        while True:
            logger.info(
                "等待短信服务返回验证码: mobile=%s, resend_attempts=%d",
                mask_phone(mobile_number.mobile_number),
                resend_attempts,
            )
            code = ctx.app_context.sms_service.get_latest_verification_code(
                mobile_number,
                sent_after=sent_after,
            )
            if code is None:
                if resend_attempts < self.MAX_RESEND_ATTEMPTS:
                    logger.warning("短信验证码获取超时，点击重发后再次等待")
                    sent_after = await self._resend_code(tab)
                    resend_attempts += 1
                    continue

                logger.warning("短信验证码最终获取失败，回调取消短信交易")
                ctx.app_context.sms_service.callback(
                    mobile_number,
                    is_verification_code_received=False,
                )
                return self._build_sms_timeout_result(ctx, await tab.current_url)

            logger.info("短信验证码已获取: %s", code)
            result = await self._submit_code_and_wait_result(tab, code)
            current_url = await tab.current_url
            if result.condition_name == self.CODE_FILL_MISMATCH_STATUS:
                ctx.app_context.sms_service.callback(
                    mobile_number,
                    is_verification_code_received=False,
                )
                return NodeResult.fail(
                    status=self.CODE_FILL_MISMATCH_STATUS,
                    error=str(result.value),
                    data=_create_result_data(code=code, current_url=current_url),
                )

            logger.debug(
                "短信验证码提交后的页面等待结果: condition=%s, current_url=%s",
                result.condition_name,
                current_url,
            )
            if result.condition_name == "consent_ready":
                logger.debug("短信验证完成，进入 Codex consent 页面")
                ctx.app_context.sms_service.callback(
                    mobile_number,
                    is_verification_code_received=True,
                )
                return NodeResult.ok(
                    status=self.SUCCESS_STATUS,
                    data=_create_result_data(code=code, current_url=current_url),
                )

            if result.condition_name == "submit_error":
                if resend_attempts < self.MAX_RESEND_ATTEMPTS:
                    logger.warning("短信验证码提交出现错误，点击重发后再次等待: error=%s", result.value)
                    sent_after = await self._resend_code(tab)
                    resend_attempts += 1
                    continue

                logger.warning("短信验证码提交错误且已无重发次数，回调取消短信交易: error=%s", result.value)
                ctx.app_context.sms_service.callback(
                    mobile_number,
                    is_verification_code_received=False,
                )
                return NodeResult.fail(
                    status=self.ERROR_STATUS,
                    error=str(result.value),
                    data=_create_result_data(code=code, current_url=current_url),
                )

            logger.warning("短信验证码提交后进入未知页面，回调取消短信交易: current_url=%s", current_url)
            ctx.app_context.sms_service.callback(
                mobile_number,
                is_verification_code_received=False,
            )
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交短信验证码后未进入 consent 页面: {current_url}",
                data=_create_result_data(code=code, current_url=current_url),
            )

    def _build_sms_timeout_result(
            self,
            ctx: RegisterContext,
            current_url: str,
    ) -> NodeResult:
        if ctx.app_context is None:
            raise RuntimeError("注册上下文缺少 AppContext，无法判断短信重试次数")

        max_retry_attempts = (
            ctx.app_context.config.register.sms_verification_retry_attempts
        )
        current_retry_count = int(
            ctx.get_value(self.SMS_VERIFICATION_RETRY_COUNT_STATE_KEY, 0) or 0
        )
        if current_retry_count >= max_retry_attempts:
            logger.warning(
                "短信验证码等待超时且已达到重试上限: retry_count=%d/%d",
                current_retry_count,
                max_retry_attempts,
            )
            return NodeResult.fail(
                status=self.TIMEOUT_STATUS,
                error="等待短信验证码超时",
                data=_create_result_data(code="", current_url=current_url),
            )

        next_retry_count = current_retry_count + 1
        logger.warning(
            "短信验证码等待超时，准备重新进入 Codex 账号选择节点: retry_count=%d/%d",
            next_retry_count,
            max_retry_attempts,
        )
        return NodeResult.ok(
            status=self.RETRY_SELECT_CODEX_ACCOUNT_STATUS,
            data={
                **_create_result_data(code="", current_url=current_url),
                self.SMS_VERIFICATION_RETRY_COUNT_STATE_KEY: next_retry_count,
            },
        )

    async def _submit_code_and_wait_result(
            self,
            tab: Tab,
            code: str,
    ) -> PydollWaitResult:
        code_input: WebElement = await tab.query(
            self.CODE_INPUT_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        try:
            await PydollClipboardInput.fill_text(
                tab,
                code_input,
                code,
                label="短信验证码输入框",
                max_attempts=self.CODE_FILL_MAX_ATTEMPTS,
            )
        except PydollClipboardInputError as exc:
            return PydollWaitResult(
                matched=True,
                condition_name=self.CODE_FILL_MISMATCH_STATUS,
                value=str(exc),
            )

        submit_button: WebElement = await tab.query(
            self.SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        logger.info("提交短信验证码")
        await submit_button.click(humanize=True)

        return await wait_for_any_condition(
            [
                PydollWaitCondition("submit_error", lambda: _query_error_text(tab)),
                PydollWaitCondition(
                    "consent_ready",
                    lambda: _current_url_contains(tab, self.CONSENT_URL_PART),
                ),
            ],
            timeout_seconds=self.SUBMIT_RESULT_WAIT_TIMEOUT_SECONDS,
        )

    async def _resend_code(self, tab: Tab) -> datetime:
        resend_button: WebElement = await tab.query(
            self.RESEND_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        resend_at = self._now()
        logger.info("点击短信验证码重发按钮")
        await resend_button.click(humanize=True)
        return resend_at


async def _query_error_text(tab: Tab) -> str | None:
    error_element: WebElement | None = await tab.query(
        WaitSmsVerificationCodeNode.ERROR_MESSAGE_SELECTOR,
        raise_exc=False,
    )
    if error_element is None:
        return None
    error_text = (await error_element.text).strip()
    return error_text or None


async def _current_url_contains(tab: Tab, url_part: str) -> str | None:
    current_url = await tab.current_url
    if url_part in current_url:
        return current_url
    return None


def _create_result_data(code: str, current_url: str) -> dict[str, object]:
    return {
        WaitSmsVerificationCodeNode.SMS_VERIFICATION_CODE_STATE_KEY: code,
        WaitSmsVerificationCodeNode.CONSENT_URL_STATE_KEY: current_url,
    }
