from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum

from pydoll.browser.tab import Tab
from pydoll.commands import InputCommands
from pydoll.elements.web_element import WebElement
from pydoll.protocol.input.types import KeyEventType, KeyModifier

logger = logging.getLogger(__name__)


class InputValueMatchMode(str, Enum):
    EXACT = "exact"
    DIGITS = "digits"


class PydollClipboardInputError(RuntimeError):
    """
    通过剪贴板写入输入框失败。
    """


@dataclass(frozen=True)
class PydollClipboardInputResult:
    text: str
    current_value: str
    method: str
    attempts: int


class PydollClipboardInput:
    """
    使用系统剪贴板向 pydoll 输入框写入文本。

    优先触发浏览器的粘贴路径；如果页面没有接收到粘贴事件，则整体替换
    input/textarea 的 value，并派发 paste/input/change 事件让前端状态同步。
    """

    DEFAULT_READ_DELAY_SECONDS = 0.2

    @classmethod
    async def fill_text(
        cls,
        tab: Tab,
        element: WebElement,
        text: str,
        *,
        label: str = "输入框",
        expected_value: str | None = None,
        match_mode: InputValueMatchMode = InputValueMatchMode.EXACT,
        max_attempts: int = 1,
        read_delay_seconds: float = DEFAULT_READ_DELAY_SECONDS,
    ) -> PydollClipboardInputResult:
        if max_attempts <= 0:
            raise ValueError("剪贴板输入最大尝试次数必须大于 0")

        expected_text = text if expected_value is None else expected_value
        last_value = ""
        for attempt in range(1, max_attempts + 1):
            logger.debug(
                "%s 通过剪贴板输入文本: attempt=%d/%d, text=%s",
                label,
                attempt,
                max_attempts,
                text,
            )
            _copy_to_clipboard(text)
            await element.clear()
            await element.click(humanize=True)
            try:
                await cls._paste_from_clipboard_with_cdp(tab)
            except Exception as exc:
                logger.warning("%s CDP 粘贴命令失败，准备使用 DOM 事件兜底: %s", label, exc)

            await asyncio.sleep(read_delay_seconds)
            last_value = await cls.read_value(element)
            if cls._matches(last_value, expected_text, match_mode):
                logger.debug(
                    "%s 剪贴板粘贴成功: current_value=%s",
                    label,
                    last_value,
                )
                return PydollClipboardInputResult(
                    text=text,
                    current_value=last_value,
                    method="clipboard_paste",
                    attempts=attempt,
                )

            logger.warning(
                "%s 剪贴板粘贴后内容不匹配，改用 DOM 事件整体写入: expected=%s, actual=%s",
                label,
                expected_text,
                last_value,
            )
            dom_result = await cls._set_value_with_dom_events(element, text)
            last_value = str(dom_result.get("value", ""))
            if cls._matches(last_value, expected_text, match_mode):
                logger.info(
                    "%s DOM 事件写入成功: current_value=%s, mode=%s",
                    label,
                    last_value,
                    dom_result.get("mode", ""),
                )
                return PydollClipboardInputResult(
                    text=text,
                    current_value=last_value,
                    method=str(dom_result.get("mode", "")),
                    attempts=attempt,
                )

            logger.warning(
                "%s DOM 事件写入后内容仍不匹配: expected=%s, actual=%s",
                label,
                expected_text,
                last_value,
            )

        raise PydollClipboardInputError(
            f"{label} 输入失败: expected={expected_text}, actual={last_value}"
        )

    @staticmethod
    async def read_value(element: WebElement) -> str:
        result = await element.execute_script(
            "return this.value || ''",
            return_by_value=True,
        )
        return str(result["result"]["result"].get("value", ""))

    @staticmethod
    async def _paste_from_clipboard_with_cdp(tab: Tab) -> None:
        paste_command = InputCommands.dispatch_key_event(
            type=KeyEventType.RAW_KEY_DOWN,
            modifiers=KeyModifier.META,
            key="v",
            code="KeyV",
            windows_virtual_key_code=86,
            native_virtual_key_code=86,
            commands=["paste"],
        )
        release_command = InputCommands.dispatch_key_event(
            type=KeyEventType.KEY_UP,
            key="v",
            code="KeyV",
            windows_virtual_key_code=86,
            native_virtual_key_code=86,
        )
        await tab._execute_command(paste_command)
        await tab._execute_command(release_command)

    @staticmethod
    async def _set_value_with_dom_events(
        element: WebElement,
        text: str,
    ) -> dict[str, object]:
        script = f"""
        function() {{
            const el = this;
            const text = {json.dumps(text)};
            el.focus();

            let pasteEventDispatched = false;
            try {{
                const clipboardData = new DataTransfer();
                clipboardData.setData("text/plain", text);
                const pasteEvent = new ClipboardEvent("paste", {{
                    bubbles: true,
                    cancelable: true,
                    clipboardData,
                }});
                pasteEventDispatched = el.dispatchEvent(pasteEvent);
            }} catch (error) {{
                pasteEventDispatched = false;
            }}

            const descriptor =
                Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")
                || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")
                || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value");
            if (descriptor && descriptor.set) {{
                descriptor.set.call(el, text);
            }} else {{
                el.value = text;
            }}
            el.selectionStart = text.length;
            el.selectionEnd = text.length;
            el.dispatchEvent(new InputEvent("input", {{
                bubbles: true,
                cancelable: true,
                data: text,
                inputType: "insertFromPaste",
            }}));
            el.dispatchEvent(new Event("change", {{ bubbles: true }}));

            return {{
                mode: "dom_value_and_events",
                paste_event_dispatched: pasteEventDispatched,
                value: el.value || "",
            }};
        }}
        """
        result = await element.execute_script(script, return_by_value=True)
        value = result["result"]["result"].get("value", {})
        return dict(value)

    @staticmethod
    def _matches(
        current_value: str,
        expected_value: str,
        match_mode: InputValueMatchMode,
    ) -> bool:
        if match_mode == InputValueMatchMode.DIGITS:
            current_digits = "".join(char for char in current_value if char.isdigit())
            expected_digits = "".join(char for char in expected_value if char.isdigit())
            return current_digits == expected_digits
        return current_value == expected_value


def _copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
