from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from core.app_context import AppContext
from core.logging_config import format_duration, mask_email
from email.email_service import EmailAccount, EmailMessage
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

from register.browser_context import CURRENT_TAB_STATE_KEY as DEFAULT_CURRENT_TAB_STATE_KEY
from register.nodes.fill_email_and_submit_node import FillEmailAndSubmitNode
from register.pydoll_clipboard_input import PydollClipboardInput
from register.pydoll_wait import (
    PydollWaitCondition,
    PydollWaitResult,
    wait_for_any_condition,
)
from register.register_flow import NodeResult, RegisterContext, RegisterNode, RetryPolicy

logger = logging.getLogger(__name__)


class WaitEmailVerificationCodeNode(RegisterNode):
    """
    等待邮箱验证码，填写提交，并进入 about-you 页面。
    """

    DEFAULT_NAME = "wait_email_verification_code"
    POLL_INTERVAL_SECONDS = 5
    EMAIL_ACCOUNT_STATE_KEY = FillEmailAndSubmitNode.EMAIL_ACCOUNT_STATE_KEY
    EMAIL_SUBMITTED_AT_STATE_KEY = FillEmailAndSubmitNode.EMAIL_SUBMITTED_AT_STATE_KEY
    CODE_INPUT_STATE_KEY = FillEmailAndSubmitNode.VERIFICATION_CODE_INPUT_STATE_KEY
    CURRENT_TAB_STATE_KEY = DEFAULT_CURRENT_TAB_STATE_KEY
    VERIFICATION_MESSAGE_STATE_KEY = "email_verification_message"
    VERIFICATION_CODE_STATE_KEY = "email_verification_code"
    ABOUT_YOU_NAME_INPUT_STATE_KEY = "about_you_name_input"
    ABOUT_YOU_AGE_INPUT_STATE_KEY = "about_you_age_input"
    ABOUT_YOU_URL_STATE_KEY = "about_you_url"
    VALIDATE_BUTTON_SELECTOR = "button[type='submit'][value='validate']"
    RESEND_BUTTON_SELECTOR = "button[type='submit'][value='resend']"
    CODE_INPUT_SELECTOR = "input[name='code']"
    TRY_AGAIN_BUTTON_SELECTOR = "button[data-dd-action-name='Try again']"
    ERROR_MESSAGE_SELECTOR = "span[slot='errorMessage']"
    ABOUT_YOU_NAME_INPUT_SELECTOR = "input[name='name']"
    ABOUT_YOU_AGE_INPUT_SELECTOR = "input[name='age']"
    CHATGPT_PROFILE_BUTTON_SELECTOR = "div[data-testid='accounts-profile-button']"
    CODEX_ADD_PHONE_URL_PART = "/add-phone"
    CODEX_CONSENT_URL_PART = "/sign-in-with-chatgpt/codex/consent"
    INVALID_CODE_TEXT = "代码不正确"
    ACCOUNT_CREATE_ERROR_TEXT = "无法创建你的帐户"
    EXPECTED_ABOUT_YOU_URL = "https://auth.openai.com/about-you"
    EXPECTED_CHATGPT_URL = "https://chatgpt.com"
    SUBMIT_RESULT_WAIT_TIMEOUT_SECONDS = 30
    SUBMIT_RESULT_POLL_INTERVAL_SECONDS = 0.5
    CODE_INPUT_READY_WAIT_TIMEOUT_SECONDS = 10
    ABOUT_YOU_READY_CONDITION = "about_you_ready"
    CHATGPT_READY_CONDITION = "chatgpt_ready"
    INVALID_CODE_CONDITION = "invalid_code"
    ACCOUNT_CREATE_ERROR_CONDITION = "account_create_error"
    CODE_INPUT_READY_CONDITION = "code_input_ready"
    TRY_AGAIN_CONDITION = "try_again"
    CODEX_NEEDS_PHONE_STATUS = "codex_oauth_needs_phone"
    CODEX_CONSENT_STATUS = "codex_oauth_consent_ready"
    SUCCESS_STATUS = "email_verified"
    CHATGPT_READY_STATUS = "email_verified_chatgpt_ready"
    RETRY_CURRENT_NODE_STATUS = "email_verification_retry_current_node"
    FAILED_STATUS = "email_verification_failed"
    TIMEOUT_STATUS = "email_verification_code_timeout"
    INVALID_ACCOUNT_STATUS = "account_create_failed"
    UNEXPECTED_URL_STATUS = "about_you_unexpected_url"

    def __init__(
            self,
            name: str = DEFAULT_NAME,
            *,
            retry_policy: RetryPolicy | None = None,
            poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
            sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
            now: Callable[[], datetime] | None = None,
            expected_about_you_url: str = EXPECTED_ABOUT_YOU_URL,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._poll_interval_seconds = poll_interval_seconds
        self._sleeper = sleeper
        self._now = now or (lambda: datetime.now(UTC))
        self._expected_about_you_url = expected_about_you_url

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
            raise RuntimeError("注册上下文缺少 AppContext，无法查询邮箱验证码")

        tab: Tab = ctx.get_value(self.CURRENT_TAB_STATE_KEY)
        email_account: EmailAccount = ctx.get_value(self.EMAIL_ACCOUNT_STATE_KEY)
        sent_after: datetime = ctx.get_value(self.EMAIL_SUBMITTED_AT_STATE_KEY)
        code_input_result = await self._wait_code_input_or_try_again(tab)
        if not code_input_result.matched:
            return NodeResult.fail(
                status=self.FAILED_STATUS,
                error="邮箱验证码页未出现验证码输入框，也未出现 Try again 按钮",
            )

        if code_input_result.condition_name == self.TRY_AGAIN_CONDITION:
            try_again_button: WebElement = code_input_result.value
            logger.warning("邮箱验证码页出现 Try again 按钮，点击后重新执行当前节点")
            await try_again_button.click(humanize=True)
            return NodeResult.ok(status=self.RETRY_CURRENT_NODE_STATUS)

        if code_input_result.condition_name in (
                self.CODEX_NEEDS_PHONE_STATUS,
                self.CODEX_CONSENT_STATUS,
        ):
            current_url = await tab.current_url
            logger.info(
                "邮箱验证码节点入口已进入 Codex 后续页面: status=%s, current_url=%s",
                code_input_result.condition_name,
                current_url,
            )
            return NodeResult.ok(
                status=code_input_result.condition_name or "",
                data={self.ABOUT_YOU_URL_STATE_KEY: current_url},
            )

        code_input: WebElement = code_input_result.value

        timeout_seconds = ctx.app_context.config.register.verification_code_wait_timeout
        deadline = self._now() + timedelta(seconds=timeout_seconds)
        logger.info(
            "开始等待邮箱验证码: email=%s, timeout=%s, interval=%s",
            mask_email(email_account.email_address),
            format_duration(timeout_seconds),
            format_duration(self._poll_interval_seconds),
        )

        while self._now() <= deadline:
            message = await self._poll_message_until_code(
                ctx.app_context,
                email_account,
                sent_after,
                deadline,
            )
            if message is None:
                break

            logger.info(
                "邮箱验证码已获取: %s",
                message.verification_code,
            )
            await PydollClipboardInput.fill_text(
                tab,
                code_input,
                message.verification_code or "",
                label="邮箱验证码输入框",
            )

            validate_button: WebElement = await tab.query(
                self.VALIDATE_BUTTON_SELECTOR,
                timeout=10,
                raise_exc=True,
            )
            await validate_button.click(humanize=True)

            wait_result = await wait_for_any_condition(
                [
                    PydollWaitCondition(
                        self.ACCOUNT_CREATE_ERROR_CONDITION,
                        lambda: _check_error_text_contains(
                            tab,
                            self.ERROR_MESSAGE_SELECTOR,
                            self.ACCOUNT_CREATE_ERROR_TEXT,
                        ),
                    ),
                    PydollWaitCondition(
                        self.INVALID_CODE_CONDITION,
                        lambda: _check_error_text_equals(
                            tab,
                            self.ERROR_MESSAGE_SELECTOR,
                            self.INVALID_CODE_TEXT,
                        ),
                    ),
                    PydollWaitCondition(
                        self.ABOUT_YOU_READY_CONDITION,
                        lambda: _check_about_you_ready(tab, self),
                    ),
                    PydollWaitCondition(
                        self.CHATGPT_READY_CONDITION,
                        lambda: _check_chatgpt_ready(tab, self),
                    ),
                    PydollWaitCondition(
                        self.CODEX_NEEDS_PHONE_STATUS,
                        lambda: _current_url_contains(tab, self.CODEX_ADD_PHONE_URL_PART),
                    ),
                    PydollWaitCondition(
                        self.CODEX_CONSENT_STATUS,
                        lambda: _current_url_contains(tab, self.CODEX_CONSENT_URL_PART),
                    ),
                ],
                timeout_seconds=self.SUBMIT_RESULT_WAIT_TIMEOUT_SECONDS,
                poll_interval_seconds=self.SUBMIT_RESULT_POLL_INTERVAL_SECONDS,
            )
            if not wait_result.matched:
                logger.warning("提交邮箱验证码后未匹配到任何页面结果")
                return NodeResult.fail(
                    status=self.FAILED_STATUS,
                    error="提交邮箱验证码后等待页面结果超时",
                    data=_create_result_data(
                        message=message,
                        current_url=await tab.current_url,
                    ),
                )

            if wait_result.condition_name == self.ACCOUNT_CREATE_ERROR_CONDITION:
                logger.error("提交邮箱验证码后出现账号创建错误")
                return NodeResult.fail(
                    status=self.INVALID_ACCOUNT_STATUS,
                    error=self.ACCOUNT_CREATE_ERROR_TEXT,
                    data=_create_result_data(
                        message=message,
                        current_url=await tab.current_url,
                    ),
                )

            if wait_result.condition_name == self.INVALID_CODE_CONDITION:
                logger.warning("邮箱验证码无效，点击重新发送后继续等待")
                resend_button: WebElement = await tab.query(
                    self.RESEND_BUTTON_SELECTOR,
                    timeout=10,
                    raise_exc=True,
                )
                sent_after = self._now()
                await resend_button.click(humanize=True)
                continue

            if wait_result.condition_name == self.CHATGPT_READY_CONDITION:
                logger.info("邮箱验证完成，页面已进入 ChatGPT 登录成功状态")
                return NodeResult.ok(
                    status=self.CHATGPT_READY_STATUS,
                    data=_create_result_data(
                        message=message,
                        current_url=await tab.current_url,
                    ),
                )

            if wait_result.condition_name in (
                    self.CODEX_NEEDS_PHONE_STATUS,
                    self.CODEX_CONSENT_STATUS,
            ):
                current_url = await tab.current_url
                logger.info(
                    "OAuth 邮箱验证完成，进入 Codex 后续页面: status=%s, current_url=%s",
                    wait_result.condition_name,
                    current_url,
                )
                return NodeResult.ok(
                    status=wait_result.condition_name or "",
                    data=_create_result_data(
                        message=message,
                        current_url=current_url,
                    ),
                )

            about_you_ready: AboutYouReadyResult = wait_result.value
            logger.info(
                "邮箱验证完成，资料页已就绪: current_url=%s",
                about_you_ready.current_url,
            )
            result_data = _create_result_data(
                message=message,
                current_url=about_you_ready.current_url,
                name_input=about_you_ready.name_input,
                age_input=about_you_ready.age_input,
            )
            if not _is_expected_url(
                    about_you_ready.current_url,
                    self._expected_about_you_url,
            ):
                return NodeResult.fail(
                    status=self.UNEXPECTED_URL_STATUS,
                    error=f"邮箱验证后 URL 不符合预期: {about_you_ready.current_url}",
                    data=result_data,
                )
            return NodeResult.ok(status=self.SUCCESS_STATUS, data=result_data)

        logger.warning("等待邮箱验证码超时: timeout=%s", format_duration(timeout_seconds))
        return NodeResult.fail(
            status=self.TIMEOUT_STATUS,
            error=f"等待邮箱验证码超时: {timeout_seconds} 秒",
        )

    async def _wait_code_input_or_try_again(
            self,
            tab: Tab,
    ) -> PydollWaitResult:
        return await wait_for_any_condition(
            [
                PydollWaitCondition(
                    self.TRY_AGAIN_CONDITION,
                    lambda: _query_element(tab, self.TRY_AGAIN_BUTTON_SELECTOR),
                ),
                PydollWaitCondition(
                    self.CODE_INPUT_READY_CONDITION,
                    lambda: _query_element(tab, self.CODE_INPUT_SELECTOR),
                ),
                PydollWaitCondition(
                    self.CODEX_NEEDS_PHONE_STATUS,
                    lambda: _current_url_contains(tab, self.CODEX_ADD_PHONE_URL_PART),
                ),
                PydollWaitCondition(
                    self.CODEX_CONSENT_STATUS,
                    lambda: _current_url_contains(tab, self.CODEX_CONSENT_URL_PART),
                ),
            ],
            timeout_seconds=self.CODE_INPUT_READY_WAIT_TIMEOUT_SECONDS,
        )

    async def _poll_message_until_code(
            self,
            app_context: AppContext,
            email_account: EmailAccount,
            sent_after: datetime,
            deadline: datetime,
    ) -> EmailMessage | None:
        poll_count = 0
        while self._now() <= deadline:
            poll_count += 1
            logger.debug(
                "查询邮箱验证码邮件: email=%s, poll=%d",
                mask_email(email_account.email_address),
                poll_count,
            )
            message = app_context.email_service.search_first_email(
                email_account,
                sent_after=sent_after,
            )
            if message is not None and message.verification_code:
                logger.debug(
                    "匹配到邮箱验证码邮件: email=%s, poll=%d",
                    mask_email(email_account.email_address),
                    poll_count,
                )
                return message
            logger.info(
                "暂未匹配到邮箱验证码邮件，等待下一轮: interval=%s",
                format_duration(self._poll_interval_seconds),
            )
            await self._sleeper(self._poll_interval_seconds)
        return None


@dataclass(frozen=True)
class AboutYouReadyResult:
    name_input: WebElement
    age_input: WebElement
    current_url: str


async def _query_element(tab: Tab, selector: str) -> WebElement | None:
    return await tab.query(selector, raise_exc=False)


async def _check_error_text_contains(
        tab: Tab,
        selector: str,
        expected_text: str,
) -> str | None:
    error_text = await _query_text(tab, selector)
    if error_text is not None and expected_text in error_text:
        return error_text
    return None


async def _check_error_text_equals(
        tab: Tab,
        selector: str,
        expected_text: str,
) -> str | None:
    error_text = await _query_text(tab, selector)
    if error_text is not None and error_text.strip() == expected_text:
        return error_text
    return None


async def _check_about_you_ready(
        tab: Tab,
        node: WaitEmailVerificationCodeNode,
) -> AboutYouReadyResult | None:
    name_input: WebElement | None = await tab.query(
        node.ABOUT_YOU_NAME_INPUT_SELECTOR,
        raise_exc=False,
    )
    if name_input is None:
        return None
    age_input: WebElement | None = await tab.query(
        node.ABOUT_YOU_AGE_INPUT_SELECTOR,
        raise_exc=False,
    )
    if age_input is None:
        return None
    return AboutYouReadyResult(
        name_input=name_input,
        age_input=age_input,
        current_url=await tab.current_url,
    )


async def _check_chatgpt_ready(
        tab: Tab,
        node: WaitEmailVerificationCodeNode,
) -> str | None:
    current_url = await tab.current_url
    if not _is_expected_chatgpt_url(current_url, node.EXPECTED_CHATGPT_URL):
        return None

    profile_button: WebElement | None = await tab.query(
        node.CHATGPT_PROFILE_BUTTON_SELECTOR,
        raise_exc=False,
    )
    if profile_button is None:
        return None
    return current_url


async def _query_text(tab: Tab, selector: str) -> str | None:
    element: WebElement | None = await tab.query(selector, raise_exc=False)
    if element is None:
        return None
    return await element.text


async def _current_url_contains(tab: Tab, url_part: str) -> str | None:
    current_url = await tab.current_url
    if url_part in current_url:
        return current_url
    return None


def _create_result_data(
        *,
        message: EmailMessage,
        current_url: str,
        name_input: WebElement | None = None,
        age_input: WebElement | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        WaitEmailVerificationCodeNode.VERIFICATION_MESSAGE_STATE_KEY: message,
        WaitEmailVerificationCodeNode.VERIFICATION_CODE_STATE_KEY: message.verification_code or "",
        WaitEmailVerificationCodeNode.ABOUT_YOU_URL_STATE_KEY: current_url,
    }
    if name_input is not None:
        result[WaitEmailVerificationCodeNode.ABOUT_YOU_NAME_INPUT_STATE_KEY] = name_input
    if age_input is not None:
        result[WaitEmailVerificationCodeNode.ABOUT_YOU_AGE_INPUT_STATE_KEY] = age_input
    return result


def _is_expected_chatgpt_url(current_url: str, expected_url: str) -> bool:
    current = urlparse(current_url)
    expected = urlparse(expected_url)
    return current.scheme == expected.scheme and current.netloc == expected.netloc


def _is_expected_url(current_url: str, expected_url: str) -> bool:
    current = urlparse(current_url)
    expected = urlparse(expected_url)
    return (
            current.scheme == expected.scheme
            and current.netloc == expected.netloc
            and current.path.rstrip("/") == expected.path.rstrip("/")
    )
