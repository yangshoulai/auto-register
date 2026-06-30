from __future__ import annotations

import asyncio
import unittest

from register.pydoll_wait import (
    PydollWaitCondition,
    element_exists_condition,
    element_text_contains_condition,
    url_matches_condition,
    wait_for_any_condition,
)


class Clock:
    def __init__(self) -> None:
        self.sleep_calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)


class FakeElement:
    def __init__(self, text: str = "") -> None:
        self._text = text

    @property
    async def text(self) -> str:
        return self._text


class FakeTab:
    def __init__(self) -> None:
        self.current_url_value = "https://chatgpt.com/"
        self.elements: dict[str, FakeElement] = {}

    async def query(self, selector: str, **kwargs) -> FakeElement | None:
        return self.elements.get(selector)

    @property
    async def current_url(self) -> str:
        return self.current_url_value


class PydollWaitTest(unittest.TestCase):
    def test_wait_returns_first_matched_condition(self) -> None:
        async def first():
            return None

        async def second():
            return "ready"

        result = asyncio.run(
            wait_for_any_condition(
                [
                    PydollWaitCondition("first", first),
                    PydollWaitCondition("second", second),
                ],
                timeout_seconds=3,
            )
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.condition_name, "second")
        self.assertEqual(result.value, "ready")
        self.assertEqual(result.elapsed_seconds, 0)

    def test_wait_polls_until_condition_matches(self) -> None:
        clock = Clock()
        calls = 0

        async def delayed():
            nonlocal calls
            calls += 1
            if calls < 3:
                return None
            return {"status": "done"}

        result = asyncio.run(
            wait_for_any_condition(
                [PydollWaitCondition("delayed", delayed)],
                timeout_seconds=5,
                poll_interval_seconds=2,
                sleeper=clock.sleep,
            )
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.condition_name, "delayed")
        self.assertEqual(result.value, {"status": "done"})
        self.assertEqual(result.elapsed_seconds, 4)
        self.assertEqual(clock.sleep_calls, [2, 2])

    def test_wait_returns_timeout_result(self) -> None:
        clock = Clock()

        async def never():
            return None

        result = asyncio.run(
            wait_for_any_condition(
                [PydollWaitCondition("never", never)],
                timeout_seconds=3,
                poll_interval_seconds=2,
                sleeper=clock.sleep,
            )
        )

        self.assertFalse(result.matched)
        self.assertIsNone(result.condition_name)
        self.assertIsNone(result.value)
        self.assertEqual(result.elapsed_seconds, 3)
        self.assertEqual(clock.sleep_calls, [2, 1])

    def test_wait_ignores_false_values(self) -> None:
        calls = 0

        async def condition():
            nonlocal calls
            calls += 1
            return False if calls == 1 else "matched"

        result = asyncio.run(
            wait_for_any_condition(
                [PydollWaitCondition("condition", condition)],
                timeout_seconds=1,
                poll_interval_seconds=1,
                sleeper=Clock().sleep,
            )
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.value, "matched")

    def test_wait_rejects_invalid_arguments(self) -> None:
        async def condition():
            return True

        with self.assertRaises(ValueError):
            asyncio.run(wait_for_any_condition([], timeout_seconds=1))

        with self.assertRaises(ValueError):
            asyncio.run(
                wait_for_any_condition(
                    [PydollWaitCondition("condition", condition)],
                    timeout_seconds=-1,
                )
            )

        with self.assertRaises(ValueError):
            asyncio.run(
                wait_for_any_condition(
                    [PydollWaitCondition("condition", condition)],
                    timeout_seconds=1,
                    poll_interval_seconds=0,
                )
            )

    def test_element_exists_condition_matches_existing_element(self) -> None:
        tab = FakeTab()
        element = FakeElement()
        tab.elements["button[type='submit']"] = element

        result = asyncio.run(
            element_exists_condition(
                "submit",
                tab,
                "button[type='submit']",
            ).check()
        )

        self.assertIs(result, element)

    def test_url_matches_condition_matches_same_location(self) -> None:
        tab = FakeTab()
        tab.current_url_value = "https://chatgpt.com/?model=gpt"

        result = asyncio.run(
            url_matches_condition(
                "chatgpt",
                tab,
                "https://chatgpt.com",
            ).check()
        )

        self.assertEqual(result, "https://chatgpt.com/?model=gpt")

    def test_element_text_contains_condition_matches_text(self) -> None:
        tab = FakeTab()
        element = FakeElement("无法创建你的帐户，请稍后再试")
        tab.elements["span[slot='errorMessage']"] = element

        result = asyncio.run(
            element_text_contains_condition(
                "account_error",
                tab,
                "span[slot='errorMessage']",
                "无法创建你的帐户",
            ).check()
        )

        self.assertIs(result, element)


if __name__ == "__main__":
    unittest.main()
