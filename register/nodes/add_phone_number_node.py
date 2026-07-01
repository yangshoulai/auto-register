from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from account.account_service import Account
from core.logging_config import mask_phone
from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.fill_email_and_submit_node import FillEmailAndSubmitNode
from register.pydoll_clipboard_input import InputValueMatchMode, PydollClipboardInput
from register.pydoll_wait import PydollWaitCondition, wait_for_any_condition
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy
from sms.sms_service import SmsMobileNumber

logger = logging.getLogger(__name__)


class AddPhoneNumberNode(RegisterNode):
    """
    在 OpenAI 手机号验证页填入短信服务手机号，并提交到短信验证码页。
    """

    DEFAULT_NAME = "add_phone_number"
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.ACCOUNT_STATE_KEY
    SMS_MOBILE_NUMBER_STATE_KEY = "sms_mobile_number"
    PHONE_SUBMITTED_AT_STATE_KEY = "phone_submitted_at"
    PHONE_VERIFICATION_URL_STATE_KEY = "phone_verification_url"
    PHONE_INPUT_SELECTOR = "input[id='tel']"
    SMS_INPUT_SELECTOR = "label > input[value='sms']"
    WHATSAPP_INPUT_SELECTOR = "label > input[value='whatsapp']"
    SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
    ERROR_MESSAGE_SELECTOR = "ul[class^='_errors_']"
    PHONE_USED_ERROR_TEXT = "电话号码已被使用"
    PHONE_INVALID_ERROR_TEXT = "电话号码无效"
    PHONE_WHATSAPP_ONLY_ERROR_TEXT = "请继续通过 WhatsApp 发送验证码"
    RETRYABLE_PHONE_ERROR_TEXTS = (
        PHONE_USED_ERROR_TEXT,
        PHONE_INVALID_ERROR_TEXT,
        PHONE_WHATSAPP_ONLY_ERROR_TEXT,
    )
    PHONE_VERIFICATION_URL_PART = "/phone-verification"
    RESULT_WAIT_TIMEOUT_SECONDS = 30
    SUCCESS_STATUS = "phone_submitted"
    FAILED_STATUS = "phone_submit_failed"
    PHONE_ERROR_STATUS = "phone_submit_error"
    MISSING_SMS_SERVICE_STATUS = "sms_service_not_configured"
    UNEXPECTED_URL_STATUS = "phone_verification_unexpected_url"

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
            raise RuntimeError("注册上下文缺少 AppContext，无法获取手机号")
        if ctx.app_context.sms_service is None:
            return NodeResult.fail(
                status=self.MISSING_SMS_SERVICE_STATUS,
                error="未配置短信服务，无法完成手机号验证",
            )

        tab: Tab = ctx.get_value(self.CURRENT_TAB_STATE_KEY)
        account: Account = ctx.get_value(self.ACCOUNT_STATE_KEY)
        phone_retry_attempts = 0
        max_phone_retry_attempts = ctx.app_context.config.register.phone_number_retry_attempts

        while True:
            result = await self._submit_new_mobile_number(ctx, tab, account)
            if not (
                    result.status == self.PHONE_ERROR_STATUS
                    and result.error is not None
                    and self._is_retryable_phone_error(result.error)
                    and phone_retry_attempts < max_phone_retry_attempts
            ):
                return result

            phone_retry_attempts += 1
            logger.warning(
                "手机号不可用，重新获取手机号后重试: error=%s, attempt=%d/%d",
                result.error,
                phone_retry_attempts,
                max_phone_retry_attempts,
            )

    def _is_retryable_phone_error(self, error_text: str) -> bool:
        return any(
            retryable_text in error_text
            for retryable_text in self.RETRYABLE_PHONE_ERROR_TEXTS
        )

    async def _submit_new_mobile_number(
            self,
            ctx: RegisterContext,
            tab: Tab,
            account: Account,
    ) -> NodeResult:
        if ctx.app_context is None or ctx.app_context.sms_service is None:
            raise RuntimeError("注册上下文缺少短信服务")

        logger.info("向短信服务申请手机号")
        mobile_number = ctx.app_context.sms_service.get_mobile_number()
        normalized_mobile_number = _normalize_mobile_number(mobile_number.mobile_number)
        account.mobile = normalized_mobile_number
        logger.info(
            "手机号已获取: mobile=%s, provider=%s",
            mask_phone(normalized_mobile_number),
            mobile_number.get_attribute("provider", ""),
        )

        phone_input: WebElement = await tab.query(
            self.PHONE_INPUT_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        logger.debug("填写手机号: mobile=%s", mask_phone(normalized_mobile_number))
        await _fill_mobile_number(tab, phone_input, normalized_mobile_number)

        await self._select_sms_verification_method(tab)

        submit_button: WebElement = await tab.query(
            self.SUBMIT_BUTTON_SELECTOR,
            timeout=10,
            raise_exc=True,
        )
        phone_submitted_at = datetime.now(UTC)
        logger.info("提交手机号验证表单")
        await submit_button.click(humanize=True)

        wait_result = await wait_for_any_condition(
            [
                PydollWaitCondition("submit_error", lambda: _query_error_text(tab)),
                PydollWaitCondition(
                    "phone_verification",
                    lambda: _current_url_contains(tab, self.PHONE_VERIFICATION_URL_PART),
                ),
            ],
            timeout_seconds=self.RESULT_WAIT_TIMEOUT_SECONDS,
        )
        current_url = await tab.current_url
        logger.info(
            "手机号提交后的页面等待结果: matched=%s, condition=%s, current_url=%s",
            wait_result.matched,
            wait_result.condition_name,
            current_url,
        )
        result_data = _create_result_data(
            mobile_number=mobile_number,
            phone_submitted_at=phone_submitted_at,
            current_url=current_url,
        )
        if not wait_result.matched:
            logger.warning("手机号提交后未进入验证码页面，回调取消短信交易")
            ctx.app_context.sms_service.callback(
                mobile_number,
                is_verification_code_received=False,
            )
            return NodeResult.fail(
                status=self.UNEXPECTED_URL_STATUS,
                error=f"提交手机号后未进入短信验证码页面: {current_url}",
                data=result_data,
            )

        if wait_result.condition_name == "submit_error":
            logger.warning("手机号提交出现页面错误，回调取消短信交易: error=%s", wait_result.value)
            ctx.app_context.sms_service.callback(
                mobile_number,
                is_verification_code_received=False,
            )
            return NodeResult.fail(
                status=self.PHONE_ERROR_STATUS,
                error=str(wait_result.value),
                data=result_data,
            )

        logger.debug("手机号验证码页面已就绪")
        return NodeResult.ok(status=self.SUCCESS_STATUS, data=result_data)

    async def _select_sms_verification_method(self, tab: Tab) -> None:
        sms_label = await _get_input_label(
            tab,
            self.SMS_INPUT_SELECTOR,
            required=False,
        )
        if sms_label is None:
            logger.info("页面未提供短信/WhatsApp 验证方式选择，按默认 SMS 方式继续")
            return

        sms_state = sms_label.get_attribute("data-state")
        if sms_state == "on":
            logger.info("短信验证方式已经选中")
            return

        logger.info("切换验证码接收方式为 SMS")
        whatsapp_label = await _get_input_label(
            tab,
            self.WHATSAPP_INPUT_SELECTOR,
            required=False,
        )
        if whatsapp_label is not None:
            await whatsapp_label.click(humanize=True)
        await sms_label.click(humanize=True)


async def _get_input_label(
        tab: Tab,
        selector: str,
        *,
        required: bool,
) -> WebElement | None:
    input_element: WebElement | None = await tab.query(
        selector,
        timeout=10,
        raise_exc=required,
    )
    if input_element is None:
        return None
    return await input_element.get_parent_element()


async def _query_error_text(tab: Tab) -> str | None:
    error_element: WebElement | None = await tab.query(
        AddPhoneNumberNode.ERROR_MESSAGE_SELECTOR,
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


async def _fill_mobile_number(
        tab: Tab,
        phone_input: WebElement,
        mobile_number: str,
) -> None:
    full_mobile_number = f"+{mobile_number}"
    await PydollClipboardInput.fill_text(
        tab,
        phone_input,
        full_mobile_number,
        label="手机号输入框",
        expected_value=mobile_number,
        match_mode=InputValueMatchMode.DIGITS,
    )


def _normalize_mobile_number(mobile_number: str) -> str:
    normalized_mobile_number = mobile_number.strip()
    if normalized_mobile_number.startswith("+"):
        return normalized_mobile_number[1:]
    return normalized_mobile_number


def _create_result_data(
        *,
        mobile_number: SmsMobileNumber,
        phone_submitted_at: datetime,
        current_url: str,
) -> dict[str, object]:
    return {
        AddPhoneNumberNode.SMS_MOBILE_NUMBER_STATE_KEY: mobile_number,
        AddPhoneNumberNode.PHONE_SUBMITTED_AT_STATE_KEY: phone_submitted_at,
        AddPhoneNumberNode.PHONE_VERIFICATION_URL_STATE_KEY: current_url,
    }
