"""会话路由：消息属于哪条会话、同一会话的消息不并发，以及**唯一出站通道**
（框架设计 §3.6）。

本模块同时是入站管线的落点（实施计划 §4.5 定死了判定顺序）：

    入站事件
      -> 去重登记（含群聊与不支持内容：便宜，先做，避免重复刷日志）
      -> 渠道实例世代校验（generation 不匹配 -> 丢弃）
      -> 聊天类型判定（非 direct -> 丢弃 + 记日志，**不回复**）
      -> 准入判定（fail-closed）
      -> 内容类型判定（text 为 None -> 明确降级文案）
      -> 命令分流（命中 -> 就地处理，**不入队**）
      -> 入队（满 -> CHANNEL_QUEUE_FULL，不入队不落库，回明确文案）

两条必须在实现里守住的纪律：

- **命令一律不入队**（§4.3）：队列在整个回合期间保持占用，`/stop` 若也进队列就永远
  停不下正在跑的回合——功能上是死的；
- **出站唯一出口**：所有 `send_text` / `edit_text` 都在本模块调用，因此同一会话的
  出站顺序天然与入站顺序一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from witty_service.channels import commands as cmd
from witty_service.channels import errors as err
from witty_service.channels.access_policy import AccessPolicy, decide, parse_policy
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    ERROR_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    ChannelAdapter,
    DeliveryResult,
    InboundMessage,
    Route,
    TurnEvent,
)
from witty_service.channels.dedup import DedupVerdict, InboundDedup
from witty_service.channels.delivery import (
    PRESENTATION_EDIT_PLACEHOLDER,
    DeliveryPlan,
    SendAction,
    plan,
)
from witty_service.domain.errors import DomainError
from witty_service.persistence.channel_repository import (
    ChannelInstanceRecord,
    ChannelRepository,
    ChannelRouteRecord,
)

logger = logging.getLogger(__name__)

#: 单条入站消息默认的队列深度（排队等待中的条数，不含执行中的那一条）
DEFAULT_QUEUE_DEPTH = 3
#: 默认停滞窗口（秒）
DEFAULT_STALL_WINDOW_SECONDS = 90.0

#: 消费回合事件时的内部标记
_EVENT = "event"
_ERROR = "error"
_END = "end"


class RouterTurnGateway(Protocol):
    """`SessionRouter` 需要的回合网关能力（由 `AgentTurnGateway` 实现）。"""

    async def resolve_session(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        channel: str | None = None,
        instance_id: str | None = None,
    ) -> str: ...

    def run_turn(
        self, agent_id: str, session_id: str, text: str
    ) -> AsyncIterator[TurnEvent]: ...

    async def abort(self, agent_id: str, session_id: str) -> None: ...

    async def reject_interaction(
        self, agent_id: str, session_id: str, request_id: str
    ) -> None: ...

    def last_assistant_text(self, session_id: str) -> str | None: ...

    def agent_state(self, agent_id: str | None) -> str: ...

    def agent_name(self, agent_id: str | None) -> str | None: ...

    def session_title(self, session_id: str | None) -> str | None: ...


@dataclass(slots=True)
class ChannelInstanceRuntime:
    """进程内的实例装配：适配器 + 连接时的世代号。"""

    instance_id: str
    channel: str
    adapter: ChannelAdapter
    generation: int
    agent_id: str | None = None


@dataclass(slots=True)
class QueuedMessage:
    turn_id: str
    text: str
    queued_at: datetime


@dataclass(slots=True)
class TurnContext:
    """一次已提交的回合：绑定闸门据此判断"结果是否还属于当前绑定"。"""

    turn_id: str
    session_id: str
    binding_version: int


@dataclass(slots=True)
class TurnOutcome:
    final_text: str | None = None
    failed: bool = False
    error_code: str | None = None
    error_message: str | None = None
    aborted: bool = False
    deferred: bool = False


@dataclass(slots=True)
class RouteState:
    """每条路由的进程内状态。

    **所有对该状态的读写都发生在事件循环内**，因此不需要锁。
    """

    route: Route
    route_record_id: str
    queue: asyncio.Queue[QueuedMessage] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    #: 绑定世代：`/new` 轮转会话时递增，用于让在飞回合的投递被拦下
    binding_version: int = 0
    current: TurnContext | None = None
    #: 当前占位消息引用（同一次处理内复用内存值，只有经历后台补发才回库读取）
    placeholder_ref: str | None = None


class SessionRouter:
    def __init__(
        self,
        *,
        repository: ChannelRepository,
        gateway: RouterTurnGateway,
        dedup: InboundDedup | None = None,
        queue_depth: int = DEFAULT_QUEUE_DEPTH,
        stall_window_seconds: float = DEFAULT_STALL_WINDOW_SECONDS,
        edit_throttle_ms: int = 0,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._dedup = dedup
        self._queue_depth = max(1, int(queue_depth))
        self._stall_window = max(0.001, float(stall_window_seconds))
        #: 占位消息的节流更新间隔：0 表示只更新开始与结束两次（MVP 的呈现方式
        #: 只有"开始"与"结束"两次更新，因此该值在 MVP 内不产生额外分支，
        #: 保留它是为了让后续切片（流式预览）不需要改配置面）
        self._edit_throttle_ms = max(0, int(edit_throttle_ms))
        self._instances: dict[str, ChannelInstanceRuntime] = {}
        self._states: dict[tuple[str, str, str], RouteState] = {}

    # ==========================================================================
    # 实例装配
    # ==========================================================================

    def register_instance(
        self,
        instance_id: str,
        *,
        channel: str,
        adapter: ChannelAdapter,
        generation: int,
        agent_id: str | None = None,
    ) -> ChannelInstanceRuntime:
        runtime = ChannelInstanceRuntime(
            instance_id=instance_id,
            channel=channel,
            adapter=adapter,
            generation=generation,
            agent_id=agent_id,
        )
        self._instances[instance_id] = runtime
        return runtime

    def unregister_instance(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)
        for key in [key for key in self._states if key[0] == instance_id]:
            state = self._states.pop(key)
            self._cancel_worker(state)

    def get_instance(self, instance_id: str) -> ChannelInstanceRuntime | None:
        return self._instances.get(instance_id)

    def update_instance_agent(self, instance_id: str, agent_id: str | None) -> None:
        runtime = self._instances.get(instance_id)
        if runtime is not None:
            runtime.agent_id = agent_id

    # ==========================================================================
    # 只读快照
    # ==========================================================================

    def queue_depth(self, route: Route) -> int:
        state = self._states.get(route.key)
        return 0 if state is None else state.queue.qsize()

    def is_still_bound(self, route: Route, session_id: str) -> bool:
        """绑定闸门：该路由当前绑定的会话，是否仍是产生结果的会话。"""
        state = self._states.get(route.key)
        if state is None:
            return False
        record = self._repository.get_route(state.route_record_id)
        if record is None:
            return False
        return record.active_session_id == session_id

    def has_running_turn(self, route: Route) -> bool:
        state = self._states.get(route.key)
        return state is not None and state.current is not None

    async def wait_until_idle(self, route: Route, *, timeout: float = 5.0) -> bool:
        """等待该路由的串行执行体退出（在飞回合与排队消息都已处理完）。

        供关闭流程与测试使用；超时返回 False，不会取消正在处理的回合。
        """
        state = self._states.get(route.key)
        worker = state.worker if state is not None else None
        if worker is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
        except TimeoutError:
            return False
        return True

    # ==========================================================================
    # 入站管线
    # ==========================================================================

    async def handle_inbound(self, message: InboundMessage) -> None:
        route = message.route
        runtime = self._instances.get(route.instance_id)
        if runtime is None:
            logger.warning(
                "Channel inbound dropped (unknown instance): instance_id=%s",
                route.instance_id,
            )
            return

        # 1) 去重登记：含群聊与不支持内容，便宜且先做，避免重复刷日志
        if not self._register_event(message):
            return

        # 2) 渠道实例世代校验：实例被删除后重建时，旧世代的长连接回调必须丢弃
        instance = self._repository.get_instance(route.instance_id)
        if instance is None:
            logger.warning(
                "Channel inbound dropped (instance gone): instance_id=%s",
                route.instance_id,
            )
            return
        if instance.generation != runtime.generation:
            logger.warning(
                "Channel inbound dropped (stale generation): instance_id=%s "
                "expected=%s actual=%s",
                route.instance_id,
                instance.generation,
                runtime.generation,
            )
            return
        # 以库中的 agent 绑定为准（PATCH 绑定后无需重连即可生效）
        runtime.agent_id = instance.agent_id

        # 3) 聊天类型判定：MVP 不放行任何群消息，丢弃且**不回复**
        #    （对一个尚未启用的场景回消息只会让用户误以为它能用）
        if route.conversation_type != CONVERSATION_TYPE_DIRECT:
            logger.info(
                "Channel inbound dropped (non-direct conversation): "
                "instance_id=%s conversation_type=%s",
                route.instance_id,
                route.conversation_type,
            )
            return

        # 4) 准入判定（fail-closed）：每次判定都从库读取，因此写入后立即生效
        command = cmd.parse_command(message.text)
        if not self._admit(instance, route, is_command=command is not None):
            # 明确降级：被拒绝也要让用户知道，而不是静默丢弃
            # （特性设计文档 7.3 把"不回复"限定为群聊这一种情况）
            await self._send_text(runtime, route, cmd.ACCESS_DENIED_TEXT)
            return

        # 5) 内容类型判定：不支持的内容必须明确降级，不静默丢弃
        if message.text is None:
            await self._send_text(
                runtime,
                route,
                cmd.render_unsupported_content(message.unsupported_kind),
            )
            return

        # 6) 命令分流：命中即就地处理，**不入队**（§4.3）
        if command is not None:
            await self._handle_command(runtime, instance, route, command)
            return

        # 7) 入队：满则不受理（不入队、不落库、回明确文案）
        state = self._state_for(route)
        depth = state.queue.qsize()
        if depth >= self._queue_depth:
            logger.info(
                "Channel queue full: instance_id=%s user=%s depth=%d",
                route.instance_id,
                route.platform_user_id,
                depth,
            )
            await self._send_text(runtime, route, cmd.render_queue_full(depth=depth))
            return
        state.queue.put_nowait(
            QueuedMessage(turn_id=str(uuid4()), text=message.text, queued_at=_now())
        )
        self._ensure_worker(state, runtime)

    def _register_event(self, message: InboundMessage) -> bool:
        if self._dedup is None:
            return True
        verdict = self._dedup.register(
            instance_id=message.route.instance_id,
            platform_event_id=message.platform_event_id,
            received_at=message.received_at,
        )
        if verdict is DedupVerdict.duplicate:
            logger.info(
                "Channel inbound dropped (duplicate): instance_id=%s event_id=%s",
                message.route.instance_id,
                message.platform_event_id,
            )
            return False
        return True

    def _admit(
        self, instance: ChannelInstanceRecord, route: Route, *, is_command: bool
    ) -> bool:
        record = self._repository.get_access_policy(
            instance_id=instance.id, conversation_type=route.conversation_type
        )
        if record is None:
            # MVP 默认放开：没有策略行 = 默认策略，不是"配置损坏"
            policy: AccessPolicy | None = AccessPolicy.open()
        else:
            policy = parse_policy(
                record.mode, record.allowlist, allow_commands=record.allow_commands
            )
        decision = decide(
            policy,
            conversation_type=route.conversation_type,
            platform_user_id=route.platform_user_id,
            is_command=is_command,
        )
        if decision.allowed:
            return True
        logger.warning(
            "Channel inbound denied by access policy: instance_id=%s user=%s reason=%s",
            instance.id,
            route.platform_user_id,
            decision.reason,
        )
        return False

    # ==========================================================================
    # 命令
    # ==========================================================================

    async def _handle_command(
        self,
        runtime: ChannelInstanceRuntime,
        instance: ChannelInstanceRecord,
        route: Route,
        command: cmd.ParsedCommand,
    ) -> None:
        if command.name == cmd.COMMAND_HELP:
            await self._send_text(
                runtime,
                route,
                cmd.render_help(agent_bound=self._agent_bound(runtime)),
            )
            return
        if command.name == cmd.COMMAND_VERSION:
            await self._send_text(
                runtime,
                route,
                cmd.render_version(
                    channel=runtime.channel,
                    adapter_version=str(
                        getattr(runtime.adapter, "adapter_version", cmd.UNKNOWN_VERSION)
                    ),
                ),
            )
            return
        if command.name == cmd.COMMAND_STATUS:
            await self._send_text(runtime, route, self._render_status(runtime, route))
            return
        if command.name == cmd.COMMAND_STOP:
            await self._handle_stop(runtime, route)
            return
        if command.name == cmd.COMMAND_NEW:
            await self._handle_new(runtime, route, instance)
            return

    def _agent_bound(self, runtime: ChannelInstanceRuntime) -> bool:
        state = self._gateway.agent_state(runtime.agent_id)
        return state not in {"unbound", "deleted"}

    def _render_status(self, runtime: ChannelInstanceRuntime, route: Route) -> str:
        binding = self._binding(route)
        session_id = binding.active_session_id if binding is not None else None
        return cmd.render_status(
            cmd.StatusView(
                agent_state=self._gateway.agent_state(runtime.agent_id),
                agent_name=self._gateway.agent_name(runtime.agent_id),
                session_id=session_id,
                session_title=self._gateway.session_title(session_id),
                queue_depth=self.queue_depth(route),
            )
        )

    async def _handle_stop(
        self, runtime: ChannelInstanceRuntime, route: Route
    ) -> None:
        """停止：清空队列 -> 逐条告知 -> 中止当前回合 -> 回执（框架设计 §4.4）。"""
        state = self._states.get(route.key)
        if state is None:
            await self._send_text(runtime, route, cmd.STOP_IDLE_ACK_TEXT)
            return
        drained = self._drain_queue(state)
        for _ in drained:
            await self._send_text(runtime, route, cmd.STOP_CANCELLED_TEXT)
        current = state.current
        if current is None or runtime.agent_id is None:
            await self._send_text(runtime, route, cmd.STOP_IDLE_ACK_TEXT)
            return
        try:
            await self._gateway.abort(runtime.agent_id, current.session_id)
        except Exception:
            # 中止失败只记警告：回执仍然要给，用户不能因为适配器不可达而"没有反应"
            logger.warning(
                "Channel abort failed: instance_id=%s session_id=%s",
                route.instance_id,
                current.session_id,
                exc_info=True,
            )
        await self._send_text(runtime, route, cmd.STOP_ACK_TEXT)

    async def _handle_new(
        self,
        runtime: ChannelInstanceRuntime,
        route: Route,
        instance: ChannelInstanceRecord,
    ) -> None:
        """新建会话：立即生效；在飞回合继续跑完，其结果的投递被绑定闸门拦下。"""
        if not runtime.agent_id:
            await self._send_text(runtime, route, cmd.AGENT_NOT_BOUND_TEXT)
            return
        try:
            new_session_id = await self._gateway.resolve_session(
                runtime.agent_id,
                None,
                channel=runtime.channel,
                instance_id=runtime.instance_id,
            )
        except DomainError as exc:
            await self._send_text(runtime, route, self._error_text(runtime, exc))
            return
        binding = self._binding(route)
        if binding is not None:
            self._repository.set_active_session(binding.id, new_session_id)
        state = self._state_for(route)
        # 递增绑定世代：在本路由上已经提交的回合，其结果不再投递
        state.binding_version += 1
        drained = self._drain_queue(state)
        await self._send_text(runtime, route, cmd.render_new_ack(drained=len(drained)))

    # ==========================================================================
    # 队列与处理
    # ==========================================================================

    def _state_for(self, route: Route) -> RouteState:
        state = self._states.get(route.key)
        if state is not None:
            return state
        record = self._repository.get_or_create_route(
            instance_id=route.instance_id,
            conversation_type=route.conversation_type,
            platform_user_id=route.platform_user_id,
        )
        state = RouteState(route=route, route_record_id=record.id)
        self._states[route.key] = state
        return state

    def _binding(self, route: Route) -> ChannelRouteRecord | None:
        state = self._states.get(route.key)
        if state is not None:
            return self._repository.get_route(state.route_record_id)
        existing = self._repository.get_or_create_route(
            instance_id=route.instance_id,
            conversation_type=route.conversation_type,
            platform_user_id=route.platform_user_id,
        )
        return existing

    def _drain_queue(self, state: RouteState) -> list[QueuedMessage]:
        drained: list[QueuedMessage] = []
        while True:
            try:
                drained.append(state.queue.get_nowait())
            except asyncio.QueueEmpty:
                return drained

    def _ensure_worker(
        self, state: RouteState, runtime: ChannelInstanceRuntime
    ) -> None:
        if state.worker is not None and not state.worker.done():
            return
        state.worker = asyncio.create_task(self._worker(state, runtime))

    def _cancel_worker(self, state: RouteState) -> None:
        if state.worker is not None and not state.worker.done():
            state.worker.cancel()
        state.worker = None

    async def _worker(
        self, state: RouteState, runtime: ChannelInstanceRuntime
    ) -> None:
        """每条路由的串行执行体：整个回合期间（含转入后台监视）保持占用。"""
        try:
            while True:
                try:
                    item = state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._process(state, runtime, item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 单条消息失败不影响该路由的后续消息
                    logger.exception(
                        "Channel message processing failed: instance_id=%s",
                        state.route.instance_id,
                    )
        finally:
            state.worker = None

    async def _process(
        self,
        state: RouteState,
        runtime: ChannelInstanceRuntime,
        item: QueuedMessage,
    ) -> None:
        route = state.route
        if not runtime.agent_id:
            await self._send_text(runtime, route, cmd.AGENT_NOT_BOUND_TEXT)
            return

        binding = self._binding(route)
        bound_session_id = binding.active_session_id if binding is not None else None
        try:
            # 提交前的自愈（§4.4）：会话行不存在或运行时标识缺失 -> 重建会话
            session_id = await self._gateway.resolve_session(
                runtime.agent_id,
                bound_session_id,
                channel=runtime.channel,
                instance_id=runtime.instance_id,
            )
        except DomainError as exc:
            await self._send_text(runtime, route, self._error_text(runtime, exc))
            return
        if session_id != bound_session_id and binding is not None:
            self._repository.set_active_session(binding.id, session_id)

        turn = TurnContext(
            turn_id=item.turn_id,
            session_id=session_id,
            binding_version=state.binding_version,
        )
        # 占位消息：出队开始处理时才发（排队期间不发）
        await self._send_placeholder(runtime, state, turn)
        state.current = turn
        try:
            outcome = await self._consume_turn(state, runtime, turn, item.text)
        finally:
            state.current = None

        if outcome.aborted:
            logger.info(
                "Channel turn aborted by user: instance_id=%s session_id=%s",
                route.instance_id,
                session_id,
            )
            await self._clear_placeholder(state)
            return

        final_text = outcome.final_text
        if final_text is None:
            final_text = self._gateway.last_assistant_text(session_id)
        if final_text is None:
            if outcome.failed:
                await self._deliver_error(runtime, state, turn, outcome)
            else:
                await self._deliver_error(
                    runtime,
                    state,
                    turn,
                    TurnOutcome(failed=True, error_code=err.CHANNEL_TURN_FAILED),
                )
            return

        # 绑定闸门：等待期间执行了 /new 时，旧回合的结果**不**投到新会话
        if not self._turn_delivery_allowed(state, turn):
            logger.info(
                "Channel delivery dropped (binding gate): instance_id=%s "
                "session_id=%s turn_id=%s",
                route.instance_id,
                session_id,
                turn.turn_id,
            )
            await self._clear_placeholder(state)
            return

        await self._deliver_final(runtime, state, turn, final_text, outcome.deferred)

    async def _consume_turn(
        self,
        state: RouteState,
        runtime: ChannelInstanceRuntime,
        turn: TurnContext,
        text: str,
    ) -> TurnOutcome:
        """消费回合事件流：停滞窗口按收到任何事件续期。

        事件消费跑在独立的 pump 任务里，本协程只对"事件队列"施加停滞窗口——
        直接给异步生成器的 __anext__ 套超时会在超时时把生成器一起取消掉，
        那样"转入后台监视"就没法继续了。
        """
        agent_id = runtime.agent_id
        if agent_id is None:  # 调用方（_process）已保证 agent 存在
            return TurnOutcome(
                failed=True, error_code=err.CHANNEL_AGENT_NOT_BOUND
            )
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        pump = asyncio.create_task(
            self._pump_turn(agent_id, turn.session_id, text, queue)
        )
        outcome = TurnOutcome()
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=self._stall_window
                    )
                except TimeoutError:
                    outcome.deferred = True
                    await self._mark_deferred(runtime, state)
                    continue
                if kind == _END:
                    break
                if kind == _ERROR:
                    if isinstance(payload, DomainError):
                        if payload.code == err.CHANNEL_TURN_ABORTED:
                            outcome.aborted = True
                        else:
                            outcome.failed = True
                            outcome.error_code = payload.code
                            outcome.error_message = payload.message
                    else:  # pragma: no cover - pump 只会放入 DomainError
                        outcome.failed = True
                    break
                event: TurnEvent = payload
                event_type = event.get("type")
                if event_type in TERMINAL_EVENT_TYPES:
                    outcome.final_text = _event_text(event)
                    break
                if event_type in ERROR_EVENT_TYPES:
                    outcome.failed = True
                    outcome.error_code, outcome.error_message = _event_error(event)
                    break
                if event_type == "question.asked":
                    await self._handle_interaction(runtime, state, turn, event)
                    continue
                # message.delta 与其余 type：只用于刷新停滞窗口，不作分支
        finally:
            if not pump.done():
                pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        return outcome

    async def _pump_turn(
        self,
        agent_id: str,
        session_id: str,
        text: str,
        queue: asyncio.Queue[tuple[str, Any]],
    ) -> None:
        try:
            async for event in self._gateway.run_turn(agent_id, session_id, text):
                await queue.put((_EVENT, event))
        except asyncio.CancelledError:
            raise
        except DomainError as exc:
            await queue.put((_ERROR, exc))
        except Exception as exc:
            logger.exception(
                "Channel turn stream failed: agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            await queue.put(
                (
                    _ERROR,
                    err.channel_turn_failed(
                        session_id=session_id, message=str(exc)
                    ),
                )
            )
        finally:
            await queue.put((_END, None))

    async def _handle_interaction(
        self,
        runtime: ChannelInstanceRuntime,
        state: RouteState,
        turn: TurnContext,
        event: TurnEvent,
    ) -> None:
        """交互请求的防御性处理：告知用户 -> 主动拒绝 -> 记录警告（§8.4）。"""
        payload = event.get("payload")
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        logger.warning(
            "Channel turn asked for interaction; rejecting: instance_id=%s "
            "session_id=%s request_id=%s",
            state.route.instance_id,
            turn.session_id,
            request_id,
        )
        await self._send_text(runtime, state.route, cmd.INTERACTION_NOTICE_TEXT)
        if isinstance(request_id, str) and runtime.agent_id:
            try:
                await self._gateway.reject_interaction(
                    runtime.agent_id, turn.session_id, request_id
                )
            except Exception:
                logger.warning(
                    "Failed to reject interaction: session_id=%s request_id=%s",
                    turn.session_id,
                    request_id,
                    exc_info=True,
                )

    # ==========================================================================
    # 出站（唯一出口）
    # ==========================================================================

    async def _send_placeholder(
        self,
        runtime: ChannelInstanceRuntime,
        state: RouteState,
        turn: TurnContext,
    ) -> None:
        """发送占位消息。

        占位消息自身发送失败**不阻断回合**（框架设计 §8.3）：`rejected` 时降级链
        已经到底（MVP 的占位消息本身就是纯文本），`uncertain` 时不重发——重发会
        让用户看到两条占位消息。
        """
        result = await runtime.adapter.send_text(state.route, cmd.PLACEHOLDER_TEXT)
        self._record_delivery(
            runtime, state, turn, result=result, segment_index=0, phase="placeholder"
        )
        if result.delivered:
            # 平台回执不一定带消息标识（企微 `aibot_send_msg` 的回执就没有 msgid）：
            # "没有 ref"只意味着无法原地改写，**不等于没送到**，不能记成失败。
            state.placeholder_ref = result.platform_message_ref or None
            if state.placeholder_ref is not None:
                self._repository.set_active_placeholder_ref(
                    state.route_record_id, state.placeholder_ref
                )
            return
        state.placeholder_ref = None
        logger.warning(
            "Channel placeholder delivery failed: instance_id=%s certainty=%s code=%s",
            state.route.instance_id,
            result.certainty,
            result.error_code,
        )

    async def _mark_deferred(
        self, runtime: ChannelInstanceRuntime, state: RouteState
    ) -> None:
        """静默超过停滞窗口：告知用户"稍后补发"，回合转入后台监视。"""
        capabilities = runtime.adapter.capabilities()
        if capabilities.can_edit_message and state.placeholder_ref is not None:
            result = await runtime.adapter.edit_text(
                state.route, state.placeholder_ref, cmd.DEFERRED_NOTICE_TEXT
            )
        else:
            # 明确降级：不能原地编辑的渠道只能补发一条通知，否则用户看不到任何变化
            result = await runtime.adapter.send_text(
                state.route, cmd.DEFERRED_NOTICE_TEXT
            )
        self._record_delivery(
            runtime, state, None, result=result, segment_index=0, phase="deferred"
        )
        logger.info(
            "Channel turn exceeded stall window; switched to deferred delivery: "
            "instance_id=%s certainty=%s",
            state.route.instance_id,
            result.certainty,
        )

    async def _deliver_final(
        self,
        runtime: ChannelInstanceRuntime,
        state: RouteState,
        turn: TurnContext,
        text: str,
        deferred: bool,
    ) -> None:
        delivery_plan = plan(text, runtime.adapter.capabilities())
        placeholder_ref = state.placeholder_ref
        if placeholder_ref is None:
            record = self._repository.get_route(state.route_record_id)
            placeholder_ref = record.active_placeholder_ref if record else None
        for action in delivery_plan.actions:
            result = await self._send_action(
                runtime, state, turn, action, delivery_plan, placeholder_ref, deferred
            )
            if not result.delivered:
                # 分段失败即停：继续发送会让用户看到"半截答案 + 后半截"
                logger.warning(
                    "Channel final delivery stopped: instance_id=%s segment=%d "
                    "certainty=%s code=%s",
                    state.route.instance_id,
                    action.index,
                    result.certainty,
                    result.error_code,
                )
                break
        await self._clear_placeholder(state)

    async def _send_action(
        self,
        runtime: ChannelInstanceRuntime,
        state: RouteState,
        turn: TurnContext,
        action: SendAction,
        delivery_plan: DeliveryPlan,
        placeholder_ref: str | None,
        deferred: bool,
    ) -> DeliveryResult:
        capabilities = runtime.adapter.capabilities()
        use_edit = (
            delivery_plan.presentation == PRESENTATION_EDIT_PLACEHOLDER
            and placeholder_ref is not None
            and capabilities.can_edit_message
        )
        if use_edit and placeholder_ref is not None:
            result = await runtime.adapter.edit_text(
                state.route, placeholder_ref, action.text
            )
        else:
            result = await runtime.adapter.send_text(state.route, action.text)
        self._record_delivery(
            runtime,
            state,
            turn,
            result=result,
            segment_index=action.index,
            phase="deferred_final" if deferred else "final",
        )
        return result

    async def _deliver_error(
        self,
        runtime: ChannelInstanceRuntime,
        state: RouteState,
        turn: TurnContext,
        outcome: TurnOutcome,
    ) -> None:
        text = cmd.render_error(code=outcome.error_code or err.CHANNEL_TURN_FAILED,
                                agent_name=self._gateway.agent_name(runtime.agent_id),
                                status=self._gateway.agent_state(runtime.agent_id))
        if not self._turn_delivery_allowed(state, turn):
            await self._clear_placeholder(state)
            return
        delivery_plan = plan(text, runtime.adapter.capabilities())
        placeholder_ref = state.placeholder_ref
        for action in delivery_plan.actions:
            result = await self._send_action(
                runtime, state, turn, action, delivery_plan, placeholder_ref, False
            )
            if not result.delivered:
                break
        await self._clear_placeholder(state)

    async def send_control_text(
        self, *, instance_id: str, route: Route, text: str
    ) -> DeliveryResult:
        """连通性测试等"非回合"出站的公共入口（框架设计 §9）。

        只发送调用方给定的固定文案：**不提交回合、不写会话历史、不改绑定**；
        投递仍按三态归档一条记录（`turn_id` 为空），便于排障。
        """
        runtime = self._instances.get(instance_id)
        if runtime is None:
            # 区分"实例不存在"与"实例在但没连上"：两者的处置完全不同
            record = self._repository.get_instance(instance_id)
            if record is None:
                raise err.channel_instance_not_found(instance_id)
            raise err.channel_instance_offline(
                instance_id=instance_id, status=record.status
            )
        state = self._state_for(route)
        result = await runtime.adapter.send_text(route, text)
        self._record_delivery(
            runtime,
            state,
            None,
            result=result,
            segment_index=0,
            phase="connectivity_test",
        )
        return result

    async def _send_text(
        self, runtime: ChannelInstanceRuntime, route: Route, text: str
    ) -> None:
        """命令回执、降级文案等"非回合"出站。"""
        result = await runtime.adapter.send_text(route, text)
        logger.info(
            "Channel text sent: instance_id=%s certainty=%s code=%s",
            route.instance_id,
            result.certainty,
            result.error_code,
        )

    def _error_text(self, runtime: ChannelInstanceRuntime, exc: DomainError) -> str:
        details = exc.details or {}
        return cmd.render_error(
            code=exc.code,
            agent_name=details.get("agent_name")
            or self._gateway.agent_name(runtime.agent_id),
            status=details.get("status") or self._gateway.agent_state(runtime.agent_id),
            depth=details.get("depth"),
            kind=details.get("kind"),
        )

    async def _clear_placeholder(self, state: RouteState) -> None:
        state.placeholder_ref = None
        try:
            self._repository.set_active_placeholder_ref(state.route_record_id, None)
        except Exception:
            logger.warning(
                "Failed to clear placeholder ref: route_id=%s",
                state.route_record_id,
                exc_info=True,
            )

    def _turn_delivery_allowed(self, state: RouteState, turn: TurnContext) -> bool:
        if state.binding_version != turn.binding_version:
            return False
        return self.is_still_bound(state.route, turn.session_id)

    def _record_delivery(
        self,
        runtime: ChannelInstanceRuntime,
        state: RouteState,
        turn: TurnContext | None,
        *,
        result: DeliveryResult,
        segment_index: int,
        phase: str,
    ) -> None:
        try:
            self._repository.record_delivery(
                instance_id=runtime.instance_id,
                route_id=state.route_record_id,
                session_id=turn.session_id if turn is not None else None,
                turn_id=turn.turn_id if turn is not None else None,
                certainty=result.certainty,
                segment_index=segment_index,
                platform_message_ref=result.platform_message_ref,
                error_code=result.error_code,
            )
        except Exception:
            # 投递记录是排障信息，落库失败不得影响出站
            logger.warning(
                "Failed to record channel delivery: instance_id=%s phase=%s",
                runtime.instance_id,
                phase,
                exc_info=True,
            )
        if not result.delivered:
            logger.warning(
                "Channel delivery not delivered: instance_id=%s phase=%s "
                "segment=%d certainty=%s code=%s",
                runtime.instance_id,
                phase,
                segment_index,
                result.certainty,
                result.error_code,
            )

    # ==========================================================================
    # 生命周期
    # ==========================================================================

    async def wait_until_all_idle(self, *, timeout: float = 5.0) -> bool:
        """等待**所有**路由的串行执行体退出（在飞回合与排队消息都已处理完）。

        供关闭流程使用：超时返回 False，**不会取消**正在处理的回合——取消会让用户
        永远收不到结果，宁可让关闭流程带超时放行（框架设计 §3.3 的优雅关闭）。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        for state in list(self._states.values()):
            worker = state.worker
            if worker is None:
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=remaining)
            except TimeoutError:
                return False
        return True

    async def shutdown(self) -> None:
        """停止接收新消息并取消在飞处理。

        排队中的消息会随进程内状态一起丢失（框架设计 §7.4 的显式取舍）。
        """
        self._instances.clear()
        states = list(self._states.values())
        self._states.clear()
        for state in states:
            self._drain_queue(state)
            self._cancel_worker(state)
        for state in states:
            worker = state.worker
            if worker is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker


def _event_text(event: TurnEvent) -> str | None:
    payload = event.get("payload")
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _event_error(event: TurnEvent) -> tuple[str | None, str | None]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        code = payload.get("code")
        message = payload.get("message")
        return (
            code if isinstance(code, str) else None,
            message if isinstance(message, str) else None,
        )
    return None, None


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DEFAULT_QUEUE_DEPTH",
    "DEFAULT_STALL_WINDOW_SECONDS",
    "ChannelInstanceRuntime",
    "QueuedMessage",
    "RouteState",
    "RouterTurnGateway",
    "SessionRouter",
    "TurnContext",
    "TurnOutcome",
]
