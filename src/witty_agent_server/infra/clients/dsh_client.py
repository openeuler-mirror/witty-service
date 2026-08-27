from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, Notification
from deepseek_harness.errors import (
    HarnessError,
    JsonRpcError,
    SdkProtocolError,
    TransportClosedError,
)

from witty_agent_server.infra.clients.base import ClientBase

logger = logging.getLogger(__name__)


_DEFAULT_PROVIDER = "deepseek-official"
_DEFAULT_MODEL = "deepseek-v4-flash"

# 消费轮询间隔：SDK ``next()`` 无超时，循环用非阻塞 ``drain()`` + sleep
# 轮询，保证软 abort 在通知停滞期也至多一个周期内生效。
_ABORT_POLL_INTERVAL_SECONDS = 0.05


class DshClientError(RuntimeError):
    """Dsh 传输层统一异常：SDK / 传输异常经此映射后向上抛出。"""

    def __init__(self, *, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _map_error_reason(exc: BaseException) -> str:
    """SDK 异常 → reason slug（供 Runtime 层 stream.error 透出）。"""
    if isinstance(exc, TransportClosedError):
        return "transport-closed"
    if isinstance(exc, JsonRpcError):
        return "json-rpc-error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, SdkProtocolError):
        return "protocol-error"
    return "harness-error"


def _derive_dsh_session_id(session_key: str) -> str:
    """session_key → dsh session id：``:`` 替换为 ``-``，避免路径字符问题。"""
    return session_key.replace(":", "-")


def _is_inbox_receipt(
    notification: Notification, session_id: str, message_id: str
) -> bool:
    """inbox receipt 判定（复用 SDK ``Session.run`` 同款守卫语义）。

    ``session.event`` 且 event.type == "agent/inbox/spliced" 且
    data.inserted 携带本轮 prompt 返回的 messageId。
    """
    if (
        notification.method != "session.event"
        or notification.payload.get("sessionId") != session_id
    ):
        return False
    event = notification.payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "agent/inbox/spliced":
        return False
    data = event.get("data")
    inserted = data.get("inserted") if isinstance(data, dict) else None
    return isinstance(inserted, list) and any(
        isinstance(item, dict) and item.get("id") == message_id for item in inserted
    )


class DshClient(ClientBase):
    """DeepSeek Harness（dsh）SDK 传输层客户端。

    包装 ``DeepSeekHarness`` 实现 ``ClientBase`` 契约，仅做传输与协议守卫
    （inbox receipt / 软 abort / 轮次终止判定）。

    线程模型：``_harness`` 的获取/替换（detach / stop）与活动引用计数由
    ``_harness_lock`` 串行化，配置更新不会杀死在途 turn；其余共享状态
    （``_aborted_sessions`` / ``_session_map``）依赖 GIL 单字节码原子性
    且派生规则确定性幂等，引入非幂等共享状态（如计数器、复合读改写）
    时需补锁。
    """

    def __init__(self, *, harness: DeepSeekHarness | None = None) -> None:
        self._harness = harness  # 生产路径经 ensure_harness() 懒建并 start
        self._session_map: dict[str, str] = {}
        self._aborted_sessions: set[str] = set()
        self._harness_lock = threading.Lock()
        self._active_harness_refs: dict[int, int] = {}
        self._retired_harnesses: dict[int, DeepSeekHarness] = {}
        self._workspace_dir: str | None = None
        self._session_root: str | None = None
        self._provider: str = _DEFAULT_PROVIDER
        self._model: str = _DEFAULT_MODEL
        self._api_key: str | None = None
        self._base_url: str | None = None
        self._max_tokens: int | None = None

    @property
    def harness(self) -> DeepSeekHarness | None:
        return self._harness

    def update_config(
        self,
        *,
        workspace_dir: str | None = None,
        session_root: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """运行时更新配置（detach 语义）：任一字段真实变更即替换 harness。

        旧 harness 无活动 turn 立即关闭，有则挂入待回收列表，由最后退出
        的生成器在 finally 中关闭；start 由 lifecycle 触发。
        """
        updates = {
            "_workspace_dir": workspace_dir,
            "_session_root": session_root,
            "_provider": provider,
            "_model": model,
            "_api_key": api_key,
            "_base_url": base_url,
            "_max_tokens": max_tokens,
        }
        changed = False
        for attr, value in updates.items():
            if value is not None and value != getattr(self, attr):
                setattr(self, attr, value)
                changed = True
        if changed:
            self._detach_harness()

    def ensure_harness(self) -> DeepSeekHarness:
        """按当前配置懒建 harness（不 start；start 由 lifecycle 触发）。"""
        if self._harness is None:
            self._harness = DeepSeekHarness(
                DeepSeekHarnessConfig(
                    cwd=self._workspace_dir,
                    session_root=self._session_root,
                    provider=self._provider,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    api_key=self._api_key,
                    base_url=self._base_url,
                )
            )
        return self._harness

    def close_harness(self) -> None:
        """关闭并丢弃当前 harness 及待回收 harness（stop 时调用；尽力而为）。"""
        with self._harness_lock:
            harness = self._harness
            self._harness = None
            retired = list(self._retired_harnesses.values())
            self._retired_harnesses.clear()
        for candidate in retired:
            self._close_harness_safely(candidate)
        if harness is not None:
            self._close_harness_safely(harness)

    # ------------------------------------------------------------------
    # ClientBase 契约
    # ------------------------------------------------------------------

    def list_agents(self) -> dict[str, Any]:
        # dsh 无 agent 概念：runtime 级即一个 agent，合成单 agent 结构。
        return {
            "defaultId": "main",
            "agents": [{"id": "main", "name": "dsh", "default": True}],
        }

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        del agent_id
        raise NotImplementedError("dsh: session listing falls back to in-memory repo")

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        del agent_id
        raise NotImplementedError("use lifecycle.probe_running() for readiness")

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        del agent_id
        raise NotImplementedError("dsh skills status is not supported")

    def create_session(self, *, session_key: str) -> None:
        """空操作：dsh session 在首次 prompt 时隐式创建，仅记录映射。"""
        self._session_map[session_key] = _derive_dsh_session_id(session_key)

    def delete_session(self, *, session_key: str) -> None:
        """尽力而为：删 session_root 下该 session 的落盘文件；删除映射。"""
        dsh_session_id = self._resolve_dsh_session_id(session_key)
        self._session_map.pop(session_key, None)
        self._aborted_sessions.discard(session_key)
        self._delete_session_files(dsh_session_id)

    def abort_session(self, *, session_key: str) -> None:
        """软 abort：置标志后活动消费循环至多一个轮询周期内 return。

        通知停滞（receipt 迟迟不至 / runtime 停发）时在下一个轮询周期
        生效；dsh 侧 turn 继续跑完（结果丢弃），新 turn 开始时清除标志。
        """
        logger.info("dsh soft abort requested: session_key=%s", session_key)
        self._aborted_sessions.add(session_key)

    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        """流式执行单轮，yield 原始 dsh notification（``{"method", "payload"}``）。

        绕过阻塞的 ``Session.run()``：subscribe → prompt（非阻塞入队）→
        消费循环。循环用公开 API ``drain()``（非阻塞）+ sleep 轮询替代
        无超时的 ``next()``，每轮先查软 abort 标志；生成器 return →
        finally 关订阅，不依赖 GC。
        """
        dsh_session_id = self._resolve_dsh_session_id(session_key)
        harness = self._acquire_harness()
        harness_client = harness.client

        # 新 turn 开始：清除上一轮遗留的软 abort 标志（spike-5：
        # 软 abort 后同 session 可继续提交新消息）。
        self._aborted_sessions.discard(session_key)

        subscription: Any = None
        try:
            subscription = harness_client.subscribe_session_notifications(
                dsh_session_id
            )
            message_id = harness_client.session_prompt(
                dsh_session_id,
                [{"type": "text", "text": message}],
                notification_subscription=subscription,
            )

            received_receipt = False
            pending: deque[Notification] = deque()
            while True:
                # abort 检查置于循环顶部（receipt 闸门之前）：任何阶段见
                # 标志即停止消费（dsh 侧 turn 跑完、结果丢弃）。
                if session_key in self._aborted_sessions:
                    return
                if not pending:
                    subscription.drain(pending.append)
                    if not pending:
                        time.sleep(_ABORT_POLL_INTERVAL_SECONDS)
                        continue
                notification = pending.popleft()
                if not received_receipt:
                    # receipt 守卫：闸门前的通知一律丢弃；receipt 自身仅
                    # 开闸不外发（复用 SDK Session.run 同款守卫）。
                    if not _is_inbox_receipt(notification, dsh_session_id, message_id):
                        continue
                    received_receipt = True
                    continue
                yield {"method": notification.method, "payload": notification.payload}
                if (
                    notification.method == "session.status"
                    and notification.payload.get("sessionId") == dsh_session_id
                    and notification.payload.get("status") == "idle"
                ):
                    return  # 轮次终止：本 session 回到 idle
        except (HarnessError, TimeoutError) as exc:
            raise DshClientError(
                reason=_map_error_reason(exc),
                message=f"dsh stream_turn failed: {exc}",
            ) from exc
        finally:
            # spike-5b：生成器被遗弃（GeneratorExit / GC）时也必须显式
            # close 订阅，否则会在 HarnessClient 中无界累积通知。
            if subscription is not None:
                try:
                    subscription.close()
                except Exception:
                    logger.warning(
                        "ignored error while closing dsh notification subscription",
                        exc_info=True,
                    )
            # detach 语义：释放对 harness 的活动引用；若该 harness 已被
            # update_config 挂入待回收列表且无其他活动 turn，则在此关闭。
            self._release_harness(harness)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _acquire_harness(self) -> DeepSeekHarness:
        """锁内原子获取当前 harness 并登记活动引用；未构建时抛 not-started。"""
        with self._harness_lock:
            harness = self._harness
            if harness is None:
                raise DshClientError(
                    reason="not-started",
                    message="dsh harness is not available; runtime not started",
                )
            hid = id(harness)
            self._active_harness_refs[hid] = self._active_harness_refs.get(hid, 0) + 1
            return harness

    def _release_harness(self, harness: DeepSeekHarness) -> None:
        """释放活动引用；若 harness 已待回收且无其他活动 turn，则关闭。"""
        should_close = False
        with self._harness_lock:
            hid = id(harness)
            refs = self._active_harness_refs.get(hid, 0)
            if refs <= 1:
                self._active_harness_refs.pop(hid, None)
                if hid in self._retired_harnesses:
                    self._retired_harnesses.pop(hid, None)
                    should_close = True
            else:
                self._active_harness_refs[hid] = refs - 1
        if should_close:
            self._close_harness_safely(harness)

    def _detach_harness(self) -> None:
        """替换当前 harness：无活动 turn 立即关闭，有则挂入待回收列表。"""
        close_now: DeepSeekHarness | None = None
        with self._harness_lock:
            harness = self._harness
            self._harness = None
            if harness is None:
                return
            if self._active_harness_refs.get(id(harness), 0) == 0:
                close_now = harness
            else:
                self._retired_harnesses[id(harness)] = harness
        if close_now is not None:
            self._close_harness_safely(close_now)

    @staticmethod
    def _close_harness_safely(harness: DeepSeekHarness) -> None:
        try:
            harness.close()
        except Exception:
            logger.warning("ignored error while closing dsh harness", exc_info=True)

    def _resolve_dsh_session_id(self, session_key: str) -> str:
        """解析 session_key 对应的 dsh session id（确定性派生，未预填时现场记录）。"""
        session_id = self._session_map.get(session_key)
        if isinstance(session_id, str) and session_id:
            return session_id
        session_id = _derive_dsh_session_id(session_key)
        self._session_map[session_key] = session_id
        return session_id

    def _delete_session_files(self, dsh_session_id: str) -> None:
        """尽力而为删除 session_root 下该 session 的落盘文件（JSONL 等）。"""
        if not self._session_root:
            return
        root = Path(self._session_root)
        if not root.is_dir():
            return
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            logger.warning(
                "dsh delete_session cannot list session_root %s: %s", root, exc
            )
            return
        prefix = f"{dsh_session_id}."
        for entry in entries:
            if entry.name != dsh_session_id and not entry.name.startswith(prefix):
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as exc:
                logger.warning(
                    "dsh delete_session best-effort removal failed for %s: %s",
                    entry,
                    exc,
                )


__all__ = ["DshClient", "DshClientError"]
