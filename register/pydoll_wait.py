from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.logging_config import format_duration
from pydoll.browser.tab import Tab
from pydoll.elements.web_element import WebElement

logger = logging.getLogger(__name__)


ConditionChecker = Callable[[], Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class PydollWaitCondition:
    """
    页面等待条件。

    checker 返回 None 或 False 表示条件未满足；返回其他值表示条件满足，
    返回值会原样放入 PydollWaitResult.value。
    """

    name: str
    checker: ConditionChecker

    async def check(self) -> Any:
        return await self.checker()


@dataclass(frozen=True)
class PydollWaitResult:
    matched: bool
    condition_name: str | None = None
    value: Any = None
    elapsed_seconds: float = 0


async def wait_for_any_condition(
    conditions: Sequence[PydollWaitCondition],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.5,
    sleeper: Sleeper = asyncio.sleep,
) -> PydollWaitResult:
    """
    轮询等待任意一个条件满足。

    每轮按 conditions 的顺序检查，先满足的条件先返回。超时时返回
    matched=False，并带上已等待时间。
    """

    if not conditions:
        raise ValueError("等待条件集合不能为空")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds 不能小于 0")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds 必须大于 0")

    condition_names = [condition.name for condition in conditions]
    logger.info(
        "开始等待页面条件: conditions=%s, timeout=%s, interval=%s",
        condition_names,
        format_duration(timeout_seconds),
        format_duration(poll_interval_seconds),
    )
    elapsed_seconds = 0.0
    while elapsed_seconds <= timeout_seconds:
        for condition in conditions:
            value = await condition.check()
            if value is not None and value is not False:
                logger.info(
                    "页面条件已满足: condition=%s, elapsed=%s",
                    condition.name,
                    format_duration(elapsed_seconds),
                )
                return PydollWaitResult(
                    matched=True,
                    condition_name=condition.name,
                    value=value,
                    elapsed_seconds=elapsed_seconds,
                )

        if elapsed_seconds >= timeout_seconds:
            break

        sleep_seconds = min(poll_interval_seconds, timeout_seconds - elapsed_seconds)
        await sleeper(sleep_seconds)
        elapsed_seconds += sleep_seconds

    logger.warning(
        "等待页面条件超时: conditions=%s, elapsed=%s",
        condition_names,
        format_duration(elapsed_seconds),
    )
    return PydollWaitResult(matched=False, elapsed_seconds=elapsed_seconds)


def element_exists_condition(
    name: str,
    tab: Tab,
    selector: str,
) -> PydollWaitCondition:
    async def checker() -> WebElement | None:
        return await tab.query(selector, raise_exc=False)

    return PydollWaitCondition(name, checker)


def url_matches_condition(
    name: str,
    tab: Tab,
    expected_url: str,
) -> PydollWaitCondition:
    async def checker() -> str | None:
        current_url = await tab.current_url
        if _same_url_location(current_url, expected_url):
            return current_url
        return None

    return PydollWaitCondition(name, checker)


def element_text_contains_condition(
    name: str,
    tab: Tab,
    selector: str,
    expected_text: str,
) -> PydollWaitCondition:
    async def checker() -> WebElement | None:
        element: WebElement | None = await tab.query(selector, raise_exc=False)
        if element is None:
            return None
        if expected_text in await element.text:
            return element
        return None

    return PydollWaitCondition(name, checker)


def _same_url_location(current_url: str, expected_url: str) -> bool:
    current = urlparse(current_url)
    expected = urlparse(expected_url)
    return (
        current.scheme == expected.scheme
        and current.netloc == expected.netloc
        and current.path.rstrip("/") == expected.path.rstrip("/")
    )
