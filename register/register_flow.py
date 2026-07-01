from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from core.app_context import AppContext
from core.logging_config import format_duration

logger = logging.getLogger(__name__)


class RegisterFlowError(RuntimeError):
    """
    注册流程定义或执行失败。
    """


@dataclass
class RegisterContext:
    """
    单次注册运行上下文。

    app_context 持有系统级服务；state 保存节点间传递的动态数据。
    """

    app_context: AppContext | None = None
    state: MutableMapping[str, Any] = field(default_factory=dict)

    def get_value(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set_value(self, key: str, value: Any) -> None:
        self.state[key] = value

    def update_values(self, values: Mapping[str, Any]) -> None:
        self.state.update(values)


@dataclass(frozen=True)
class NodeResult:
    """
    单个注册节点的执行结果。

    success 表示节点是否满足自己的成功预期；status 用于后续流转条件。
    """

    success: bool
    status: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def ok(
            cls,
            status: str = "success",
            data: Mapping[str, Any] | None = None,
    ) -> NodeResult:
        return cls(success=True, status=status, data=data or {})

    @classmethod
    def fail(
            cls,
            status: str = "failed",
            *,
            error: str | None = None,
            data: Mapping[str, Any] | None = None,
    ) -> NodeResult:
        return cls(success=False, status=status, data=data or {}, error=error)


@dataclass(frozen=True)
class RetryPolicy:
    """
    节点重试策略。

    max_attempts 包含第一次执行；retryable_statuses 为空时所有失败结果都可重试。
    """

    max_attempts: int = 1
    interval_seconds: float = 0
    retryable_statuses: frozenset[str] | None = None
    retry_on_exception: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        if self.interval_seconds < 0:
            raise ValueError("interval_seconds 不能小于 0")

    def can_retry_result(self, result: NodeResult) -> bool:
        if result.success:
            return False
        if self.retryable_statuses is None:
            return True
        return result.status in self.retryable_statuses


class RegisterNode(ABC):
    """
    注册流程节点。

    子类在 execute 内部完成动作和成功预期判断，并返回 NodeResult。
    """

    def __init__(
            self,
            name: str,
            *,
            retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not name:
            raise ValueError("节点名称不能为空")
        self.name = name
        self.retry_policy = retry_policy or RetryPolicy()

    @abstractmethod
    async def execute(self, ctx: RegisterContext) -> NodeResult:
        """
        执行节点动作并返回结果。
        """


class RegisterContextInitializer(Protocol):
    async def initialize(self, ctx: RegisterContext) -> None:
        """
        在流程第一个节点执行前初始化运行上下文。
        """


TransitionCondition = Callable[[NodeResult, RegisterContext], bool]


@dataclass(frozen=True)
class Transition:
    """
    节点流转规则。

    target_node 为 None 时表示流程成功结束；多个规则按配置顺序匹配。
    """

    target_node: str | None
    condition: TransitionCondition
    description: str = ""

    def matches(self, result: NodeResult, ctx: RegisterContext) -> bool:
        return self.condition(result, ctx)

    @classmethod
    def always(
            cls,
            target_node: str | None,
            *,
            description: str = "",
    ) -> Transition:
        return cls(
            target_node=target_node,
            condition=lambda result, ctx: True,
            description=description,
        )

    @classmethod
    def when_status(
            cls,
            status: str,
            target_node: str | None,
            *,
            description: str = "",
    ) -> Transition:
        return cls(
            target_node=target_node,
            condition=lambda result, ctx: result.status == status,
            description=description,
        )


@dataclass(frozen=True)
class RegisterFlow:
    """
    注册流程定义。
    """

    start_node: str
    nodes: Mapping[str, RegisterNode]
    transitions: Mapping[str, Sequence[Transition]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_node not in self.nodes:
            raise RegisterFlowError(f"注册流程起点不存在: {self.start_node}")

        for node_name, node in self.nodes.items():
            if node.name != node_name:
                raise RegisterFlowError(
                    f"节点映射键 {node_name} 与节点名称 {node.name} 不一致"
                )

        for source_node, transitions in self.transitions.items():
            if source_node not in self.nodes:
                raise RegisterFlowError(f"流转源节点不存在: {source_node}")

            for transition in transitions:
                target_node = transition.target_node
                if target_node is not None and target_node not in self.nodes:
                    raise RegisterFlowError(f"流转目标节点不存在: {target_node}")

    def get_node(self, node_name: str) -> RegisterNode:
        try:
            return self.nodes[node_name]
        except KeyError as exc:
            raise RegisterFlowError(f"注册流程节点不存在: {node_name}") from exc

    def find_next_node(
            self,
            node_name: str,
            result: NodeResult,
            ctx: RegisterContext,
    ) -> str | None:
        transition = self.find_next_transition(node_name, result, ctx)
        if transition is None:
            return None
        return transition.target_node

    def find_next_transition(
            self,
            node_name: str,
            result: NodeResult,
            ctx: RegisterContext,
    ) -> Transition | None:
        for transition in self.transitions.get(node_name, ()):
            if transition.matches(result, ctx):
                return transition
        return None


@dataclass(frozen=True)
class NodeAttempt:
    node_name: str
    attempt: int
    result: NodeResult


@dataclass(frozen=True)
class RegisterFlowResult:
    success: bool
    final_node: str
    final_result: NodeResult
    attempts: tuple[NodeAttempt, ...]

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(attempt.node_name for attempt in self.attempts)


class RegisterFlowRunner:
    """
    注册流程执行器。
    """

    def __init__(
            self,
            *,
            max_steps: int = 100,
            sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
            context_initializers: Sequence[RegisterContextInitializer] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于等于 1")
        self._max_steps = max_steps
        self._sleeper = sleeper
        self._context_initializers = tuple(context_initializers or ())

    async def run(
            self,
            flow: RegisterFlow,
            ctx: RegisterContext | None = None,
    ) -> RegisterFlowResult:
        runtime_ctx = ctx or RegisterContext()
        logger.info(
            "注册流程开始: start=%s, nodes=%d, max_steps=%d",
            flow.start_node,
            len(flow.nodes),
            self._max_steps,
        )
        await self._initialize_context(runtime_ctx)
        logger.debug(
            "注册流程上下文初始化完成: state_keys=%s",
            sorted(runtime_ctx.state.keys()),
        )
        attempts: list[NodeAttempt] = []
        current_node_name = flow.start_node

        for step_index in range(1, self._max_steps + 1):
            node = flow.get_node(current_node_name)
            logger.info(
                "流程步骤开始: step=%d, node=%s",
                step_index,
                current_node_name,
            )
            result = await self._execute_node_with_retry(node, runtime_ctx, attempts)
            runtime_ctx.update_values(result.data)
            if result.data:
                logger.debug(
                    "节点数据写入上下文: node=%s, data_keys=%s",
                    current_node_name,
                    sorted(result.data.keys()),
                )

            if not result.success:
                logger.error(
                    "注册流程失败: node=%s, status=%s, error=%s",
                    current_node_name,
                    result.status,
                    result.error or "",
                )
                return RegisterFlowResult(
                    success=False,
                    final_node=current_node_name,
                    final_result=result,
                    attempts=tuple(attempts),
                )

            transition = flow.find_next_transition(
                current_node_name,
                result,
                runtime_ctx,
            )
            if transition is None:
                logger.info(
                    "注册流程成功结束: final_node=%s, status=%s",
                    current_node_name,
                    result.status,
                )
                return RegisterFlowResult(
                    success=True,
                    final_node=current_node_name,
                    final_result=result,
                    attempts=tuple(attempts),
                )

            next_node_name = transition.target_node
            if next_node_name is None:
                logger.debug(
                    "注册流程成功结束: final_node=%s, status=%s",
                    current_node_name,
                    result.status,
                )
                return RegisterFlowResult(
                    success=True,
                    final_node=current_node_name,
                    final_result=result,
                    attempts=tuple(attempts),
                )

            logger.info(
                "节点流转: from=%s, status=%s, to=%s%s",
                current_node_name,
                result.status,
                next_node_name,
                f", description={transition.description}"
                if transition.description
                else "",
            )
            current_node_name = next_node_name

        raise RegisterFlowError(f"注册流程超过最大步数限制: {self._max_steps}")

    async def _initialize_context(self, ctx: RegisterContext) -> None:
        for initializer in self._context_initializers:
            initializer_name = type(initializer).__name__
            logger.debug("初始化注册上下文: initializer=%s", initializer_name)
            await initializer.initialize(ctx)
            logger.debug("注册上下文初始化器完成: initializer=%s", initializer_name)

    async def _execute_node_with_retry(
            self,
            node: RegisterNode,
            ctx: RegisterContext,
            attempts: list[NodeAttempt],
    ) -> NodeResult:
        retry_policy = node.retry_policy

        for attempt_index in range(1, retry_policy.max_attempts + 1):
            logger.info(
                "节点执行开始: node=%s, attempt=%d/%d",
                node.name,
                attempt_index,
                retry_policy.max_attempts,
            )
            started_at = perf_counter()
            result = await self._execute_node_once(node, ctx)
            elapsed_text = format_duration(perf_counter() - started_at)
            if result.success:
                logger.info(
                    "节点执行成功: node=%s, attempt=%d, status=%s, elapsed=%s, data_keys=%s",
                    node.name,
                    attempt_index,
                    result.status,
                    elapsed_text,
                    sorted(result.data.keys()),
                )
            else:
                logger.warning(
                    "节点执行失败: node=%s, attempt=%d, status=%s, elapsed=%s, error=%s",
                    node.name,
                    attempt_index,
                    result.status,
                    elapsed_text,
                    result.error or "",
                )
            attempts.append(
                NodeAttempt(
                    node_name=node.name,
                    attempt=attempt_index,
                    result=result,
                )
            )

            has_remaining_attempts = attempt_index < retry_policy.max_attempts
            if result.success:
                return result
            if not has_remaining_attempts:
                return result
            if not retry_policy.can_retry_result(result):
                return result

            if retry_policy.interval_seconds > 0:
                logger.info(
                    "节点等待后重试: node=%s, interval=%s",
                    node.name,
                    format_duration(retry_policy.interval_seconds),
                )
                await self._sleeper(retry_policy.interval_seconds)
            else:
                logger.info("节点立即重试: node=%s", node.name)

        raise RegisterFlowError(f"节点 {node.name} 没有产生执行结果")

    async def _execute_node_once(
            self,
            node: RegisterNode,
            ctx: RegisterContext,
    ) -> NodeResult:
        try:
            result = await node.execute(ctx)
        except Exception as exc:
            if not node.retry_policy.retry_on_exception:
                raise
            logger.exception("节点执行抛出异常: node=%s", node.name)
            return NodeResult.fail(
                status="exception",
                error=f"{type(exc).__name__}: {exc}",
            )

        if not isinstance(result, NodeResult):
            raise RegisterFlowError(
                f"节点 {node.name} 必须返回 NodeResult，实际返回 "
                f"{type(result).__name__}"
            )
        return result
