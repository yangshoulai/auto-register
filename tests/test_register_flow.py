from __future__ import annotations

import asyncio
import unittest

from register.register_flow import (
    NodeResult,
    RegisterContext,
    RegisterFlow,
    RegisterFlowError,
    RegisterFlowRunner,
    RegisterNode,
    RetryPolicy,
    Transition,
)


class ScriptedNode(RegisterNode):
    def __init__(
        self,
        name: str,
        results: list[NodeResult | Exception],
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(name, retry_policy=retry_policy)
        self._results = results

    async def execute(self, ctx: RegisterContext) -> NodeResult:
        if not self._results:
            raise AssertionError(f"节点 {self.name} 没有剩余脚本结果")

        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StateInitializer:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value
        self.calls = 0

    async def initialize(self, ctx: RegisterContext) -> None:
        self.calls += 1
        ctx.set_value(self.key, self.value)


class RegisterFlowTest(unittest.TestCase):
    def test_runner_executes_linear_flow_until_no_next_node(self) -> None:
        ctx = RegisterContext()
        start = ScriptedNode(
            "start",
            [NodeResult.ok(status="next", data={"email": "user@example.com"})],
        )
        finish = ScriptedNode(
            "finish",
            [NodeResult.ok(status="done", data={"registered": True})],
        )
        flow = RegisterFlow(
            start_node="start",
            nodes={
                "start": start,
                "finish": finish,
            },
            transitions={
                "start": [Transition.when_status("next", "finish")],
            },
        )

        result = asyncio.run(RegisterFlowRunner().run(flow, ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.final_node, "finish")
        self.assertEqual(result.final_result.status, "done")
        self.assertEqual(result.path, ("start", "finish"))
        self.assertEqual(ctx.get_value("email"), "user@example.com")
        self.assertTrue(ctx.get_value("registered"))

    def test_runner_runs_context_initializers_before_first_node(self) -> None:
        initializer = StateInitializer("started", "yes")

        class ReadStateNode(RegisterNode):
            async def execute(self, ctx: RegisterContext) -> NodeResult:
                return NodeResult.ok(data={"seen_started": ctx.get_value("started")})

        ctx = RegisterContext()
        flow = RegisterFlow(
            start_node="read_state",
            nodes={"read_state": ReadStateNode("read_state")},
        )

        result = asyncio.run(
            RegisterFlowRunner(context_initializers=[initializer]).run(flow, ctx)
        )

        self.assertTrue(result.success)
        self.assertEqual(initializer.calls, 1)
        self.assertEqual(ctx.get_value("started"), "yes")
        self.assertEqual(ctx.get_value("seen_started"), "yes")

    def test_runner_routes_by_first_matching_transition(self) -> None:
        start = ScriptedNode("start", [NodeResult.ok(status="need_sms")])
        email_code = ScriptedNode("email_code", [NodeResult.ok(status="done")])
        sms_code = ScriptedNode("sms_code", [NodeResult.ok(status="done")])
        flow = RegisterFlow(
            start_node="start",
            nodes={
                "start": start,
                "email_code": email_code,
                "sms_code": sms_code,
            },
            transitions={
                "start": [
                    Transition.when_status("need_email", "email_code"),
                    Transition.when_status("need_sms", "sms_code"),
                ],
            },
        )

        result = asyncio.run(RegisterFlowRunner().run(flow))

        self.assertTrue(result.success)
        self.assertEqual(result.final_node, "sms_code")
        self.assertEqual(result.path, ("start", "sms_code"))

    def test_runner_retries_failed_node_until_success(self) -> None:
        flaky = ScriptedNode(
            "flaky",
            [
                NodeResult.fail(status="not_ready", error="页面未加载"),
                NodeResult.ok(status="loaded"),
            ],
            retry_policy=RetryPolicy(max_attempts=2),
        )
        flow = RegisterFlow(start_node="flaky", nodes={"flaky": flaky})

        result = asyncio.run(RegisterFlowRunner().run(flow))

        self.assertTrue(result.success)
        self.assertEqual(result.final_result.status, "loaded")
        self.assertEqual(result.path, ("flaky", "flaky"))
        self.assertEqual([attempt.attempt for attempt in result.attempts], [1, 2])

    def test_runner_stops_when_failure_is_not_retryable(self) -> None:
        node = ScriptedNode(
            "submit",
            [
                NodeResult.fail(status="blocked", error="账号被拦截"),
                NodeResult.ok(status="done"),
            ],
            retry_policy=RetryPolicy(
                max_attempts=2,
                retryable_statuses=frozenset({"timeout"}),
            ),
        )
        flow = RegisterFlow(start_node="submit", nodes={"submit": node})

        result = asyncio.run(RegisterFlowRunner().run(flow))

        self.assertFalse(result.success)
        self.assertEqual(result.final_node, "submit")
        self.assertEqual(result.final_result.status, "blocked")
        self.assertEqual(result.path, ("submit",))

    def test_runner_retries_node_exceptions_as_failed_results(self) -> None:
        node = ScriptedNode(
            "wait_code",
            [
                RuntimeError("接口暂时失败"),
                NodeResult.ok(status="code_received", data={"code": "123456"}),
            ],
            retry_policy=RetryPolicy(max_attempts=2),
        )
        ctx = RegisterContext()
        flow = RegisterFlow(start_node="wait_code", nodes={"wait_code": node})

        result = asyncio.run(RegisterFlowRunner().run(flow, ctx))

        self.assertTrue(result.success)
        self.assertEqual(result.path, ("wait_code", "wait_code"))
        self.assertEqual(result.attempts[0].result.status, "exception")
        self.assertEqual(ctx.get_value("code"), "123456")

    def test_flow_rejects_transition_to_unknown_node(self) -> None:
        node = ScriptedNode("start", [NodeResult.ok()])

        with self.assertRaises(RegisterFlowError):
            RegisterFlow(
                start_node="start",
                nodes={"start": node},
                transitions={"start": [Transition.always("missing")]},
            )

    def test_runner_rejects_non_node_result(self) -> None:
        class BrokenNode(RegisterNode):
            async def execute(self, ctx: RegisterContext):
                return "not-result"

        flow = RegisterFlow(start_node="broken", nodes={"broken": BrokenNode("broken")})

        with self.assertRaises(RegisterFlowError):
            asyncio.run(RegisterFlowRunner().run(flow))


if __name__ == "__main__":
    unittest.main()
