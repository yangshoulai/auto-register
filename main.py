from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from account.account_service import Account
from core.app_context import AppContext, create_app_context
from core.config import CONFIG_PATH, load_config
from core.logging_config import configure_logging
from email.email_service import EmailAccount
from register import (
    AddPhoneNumberNode,
    CreatePasswordNode,
    FillAboutYouNode,
    FillEmailAndSubmitNode,
    OpenChatGptTabNode,
    PYDOLL_BROWSER_STATE_KEY,
    PYDOLL_INITIAL_TAB_STATE_KEY,
    PydollBrowserContextInitializer,
    RegisterContext,
    RegisterFlow,
    RegisterFlowError,
    RegisterFlowResult,
    RegisterFlowRunner,
    SelectCodexAccountNode,
    SubmitCodexConsentNode,
    Transition,
    WaitEmailVerificationCodeNode,
    WaitSmsVerificationCodeNode,
)
from register.local_callback_server import LocalCallbackServer
from sms.sms_service import SmsMobileNumber

logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI 注册自动化入口")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="配置文件路径，默认读取项目根目录下的 config.toml",
    )
    return parser.parse_args(argv)


def build_register_flow() -> RegisterFlow:
    """
    组装注册流程。

    当前流程：打开注册弹窗，提交邮箱，必要时创建密码，验证邮箱，填写资料，完成 Codex OAuth 导出。
    """
    open_chatgpt_tab_node = OpenChatGptTabNode()
    fill_email_and_submit_node = FillEmailAndSubmitNode()
    create_password_node = CreatePasswordNode()
    wait_email_verification_code_node = WaitEmailVerificationCodeNode()
    fill_about_you_node = FillAboutYouNode()
    select_codex_account_node = SelectCodexAccountNode()
    add_phone_number_node = AddPhoneNumberNode()
    wait_sms_verification_code_node = WaitSmsVerificationCodeNode()
    submit_codex_consent_node = SubmitCodexConsentNode()
    return RegisterFlow(
        start_node=open_chatgpt_tab_node.name,
        nodes={
            open_chatgpt_tab_node.name: open_chatgpt_tab_node,
            fill_email_and_submit_node.name: fill_email_and_submit_node,
            create_password_node.name: create_password_node,
            wait_email_verification_code_node.name: wait_email_verification_code_node,
            fill_about_you_node.name: fill_about_you_node,
            select_codex_account_node.name: select_codex_account_node,
            add_phone_number_node.name: add_phone_number_node,
            wait_sms_verification_code_node.name: wait_sms_verification_code_node,
            submit_codex_consent_node.name: submit_codex_consent_node,
        },
        transitions={
            open_chatgpt_tab_node.name: [
                Transition.when_status(
                    OpenChatGptTabNode.SUCCESS_STATUS,
                    fill_email_and_submit_node.name,
                )
            ],
            fill_email_and_submit_node.name: [
                Transition.when_status(
                    FillEmailAndSubmitNode.SUCCESS_STATUS,
                    wait_email_verification_code_node.name,
                ),
                Transition.when_status(
                    FillEmailAndSubmitNode.SMS_VERIFICATION_READY_STATUS,
                    wait_sms_verification_code_node.name,
                ),
                Transition.when_status(
                    FillEmailAndSubmitNode.CREATE_PASSWORD_READY_STATUS,
                    create_password_node.name,
                )
            ],
            create_password_node.name: [
                Transition.when_status(
                    CreatePasswordNode.SUCCESS_STATUS,
                    wait_email_verification_code_node.name,
                )
            ],
            wait_email_verification_code_node.name: [
                Transition.when_status(
                    WaitEmailVerificationCodeNode.RETRY_CURRENT_NODE_STATUS,
                    wait_email_verification_code_node.name,
                ),
                Transition.when_status(
                    WaitEmailVerificationCodeNode.SUCCESS_STATUS,
                    fill_about_you_node.name,
                ),
                Transition.when_status(
                    WaitEmailVerificationCodeNode.CHATGPT_READY_STATUS,
                    select_codex_account_node.name,
                ),
                Transition.when_status(
                    WaitEmailVerificationCodeNode.CODEX_NEEDS_PHONE_STATUS,
                    add_phone_number_node.name,
                ),
                Transition.when_status(
                    WaitEmailVerificationCodeNode.CODEX_CONSENT_STATUS,
                    submit_codex_consent_node.name,
                )
            ],
            fill_about_you_node.name: [
                Transition.when_status(
                    FillAboutYouNode.SUCCESS_STATUS,
                    select_codex_account_node.name,
                )
            ],
            select_codex_account_node.name: [
                Transition.when_status(
                    SelectCodexAccountNode.SUCCESS_EMAIL_VERIFICATION_READY_STATUS,
                    wait_email_verification_code_node.name,
                ),
                Transition.when_status(
                    SelectCodexAccountNode.SUCCESS_NEEDS_PHONE_STATUS,
                    add_phone_number_node.name,
                ),
                Transition.when_status(
                    SelectCodexAccountNode.SUCCESS_CONSENT_STATUS,
                    submit_codex_consent_node.name,
                ),
            ],
            add_phone_number_node.name: [
                Transition.when_status(
                    AddPhoneNumberNode.SUCCESS_STATUS,
                    wait_sms_verification_code_node.name,
                )
            ],
            wait_sms_verification_code_node.name: [
                Transition.when_status(
                    WaitSmsVerificationCodeNode.RETRY_SELECT_CODEX_ACCOUNT_STATUS,
                    select_codex_account_node.name,
                ),
                Transition.when_status(
                    WaitSmsVerificationCodeNode.SUCCESS_STATUS,
                    submit_codex_consent_node.name,
                )
            ],
        },
    )


async def run_register_flow(app_context: AppContext) -> RegisterFlowResult:
    register_context = RegisterContext(app_context=app_context)
    callback_server = LocalCallbackServer()
    logger.debug("启动本地 OAuth 回调服务: %s", callback_server.url)
    callback_server.start()
    runner = RegisterFlowRunner(
        context_initializers=[
            PydollBrowserContextInitializer(),
        ],
    )
    try:
        return await runner.run(build_register_flow(), register_context)
    finally:
        try:
            await close_register_runtime(register_context)
        finally:
            logger.debug("关闭本地 OAuth 回调服务: %s", callback_server.url)
            callback_server.stop()


async def close_register_runtime(register_context: RegisterContext) -> None:
    browser = register_context.get_value(PYDOLL_BROWSER_STATE_KEY)
    if browser is None:
        logger.info("浏览器运行时不存在，跳过关闭")
        return

    if register_context.get_value(PYDOLL_INITIAL_TAB_STATE_KEY) is None:
        logger.info("浏览器 TAB 未初始化，直接停止浏览器进程")
        stop_browser_process(browser)
        return

    with suppress(Exception):
        logger.debug("关闭 pydoll 浏览器")
        await browser.stop()
        return

    logger.warning("pydoll 浏览器正常关闭失败，尝试停止浏览器进程")
    stop_browser_process(browser)


def stop_browser_process(browser) -> None:
    with suppress(Exception):
        browser._browser_process_manager.stop_process()


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    configure_logging(
        level=config.logging.level,
        use_colors=config.logging.use_colors,
    )
    logger.info("加载配置文件: %s", Path(args.config).resolve())

    with create_app_context(config) as ctx:
        logger.debug("应用上下文初始化完成")
        try:
            result = asyncio.run(run_register_flow(ctx))
        except RegisterFlowError as exc:
            logger.error("注册流程启动失败: %s", exc)
            return

        if result.success:
            logger.info("注册流程执行完成", )
            log_registered_account_summary(result)
            return

        logger.error(
            "注册流程执行失败: 失败节点=%s, 状态=%s, 错误=%s",
            result.final_node,
            result.final_result.status,
            result.final_result.error or "",
        )


def log_registered_account_summary(result: RegisterFlowResult) -> None:
    flow_data = _collect_success_flow_data(result)
    account = _read_typed_value(flow_data, FillEmailAndSubmitNode.ACCOUNT_STATE_KEY, Account)
    email_account = _read_typed_value(
        flow_data,
        FillEmailAndSubmitNode.EMAIL_ACCOUNT_STATE_KEY,
        EmailAccount,
    )
    sms_mobile_number = _read_typed_value(
        flow_data,
        AddPhoneNumberNode.SMS_MOBILE_NUMBER_STATE_KEY,
        SmsMobileNumber,
    )

    if account is None and email_account is None and sms_mobile_number is None:
        logger.warning("注册成功，但流程结果中缺少账号摘要数据")
        return

    email_address = _first_not_empty(
        email_account.email_address if email_account is not None else None,
        account.email_address if account is not None else None,
    )
    mobile_number = _first_not_empty(
        account.mobile if account is not None else None,
        sms_mobile_number.mobile_number if sms_mobile_number is not None else None,
    )
    email_verification_code = str(
        flow_data.get(WaitEmailVerificationCodeNode.VERIFICATION_CODE_STATE_KEY) or ""
    )
    sms_verification_code = str(
        flow_data.get(WaitSmsVerificationCodeNode.SMS_VERIFICATION_CODE_STATE_KEY) or ""
    )
    first_name = account.first_name if account is not None else ""
    last_name = account.last_name if account is not None else ""
    username = " ".join(part for part in (first_name, last_name) if part)

    logger.info(
        "注册结果\n"
        "==================== 注册账号信息 ====================\n"
        "邮箱地址      : %s\n"
        "手机号        : %s\n"
        "短信验证码    : %s\n"
        "邮箱验证码    : %s\n"
        "用户名        : %s\n"
        "名            : %s\n"
        "姓            : %s\n"
        "年龄          : %s\n"
        "密码          : %s\n"
        "====================================================",
        email_address or "未记录",
        mobile_number or "未使用手机号验证",
        sms_verification_code or "未使用短信验证",
        email_verification_code or "未使用邮箱验证码",
        username or "未记录",
        first_name or "未记录",
        last_name or "未记录",
        account.age if account is not None else "未记录",
        account.password if account is not None else "未记录",
    )


def _collect_success_flow_data(result: RegisterFlowResult) -> dict[str, Any]:
    flow_data: dict[str, Any] = {}
    for attempt in result.attempts:
        if attempt.result.success:
            flow_data.update(attempt.result.data)
    return flow_data


def _read_typed_value(
        values: dict[str, Any],
        key: str,
        expected_type: type[Any],
) -> Any | None:
    value = values.get(key)
    if isinstance(value, expected_type):
        return value
    return None


def _first_not_empty(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        string_value = str(value)
        if string_value:
            return string_value
    return ""


if __name__ == "__main__":
    main()
