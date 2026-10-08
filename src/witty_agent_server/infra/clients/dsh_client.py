from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
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


@dataclass(frozen=True)
class DshModelConfig:
    """dsh 模型配置的成组契约（wire ``config["dsh"]`` 的模型面）。

    ``apply_model_config`` 按 ``==`` 整组替换：None 即显式清除该字段，由
    归一化兜底（provider/model 回落默认值），不存在「未传」与「显式 None」
    的区分——「未传」即「不调用」。
    """

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int | None = None
    # 非 deepseek 路由的 pi-ai 物化参数（api_key_env / api / base_url），
    # None 表示走 deepseek-official 原生路径（DEEPSEEK_* env）。
    provider_route: dict[str, Any] | None = None

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


def derive_dsh_session_id(session_key: str) -> str:
    """session_key → dsh session id：``:`` 替换为 ``-``，避免路径字符问题。

    DshRuntime 依赖同一派生规则做子 session 事件过滤，保持公有。
    """
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
        self._dsh_home: str | None = None
        # 模型配置成组持有（已归一化：provider/model 恒非 None）。
        self._model_config = DshModelConfig(
            provider=_DEFAULT_PROVIDER, model=_DEFAULT_MODEL
        )

    @property
    def harness(self) -> DeepSeekHarness | None:
        return self._harness

    def update_paths(
        self, *, workspace_dir: str | None = None, dsh_home: str | None = None
    ) -> None:
        """幂等同步实例目录（update_config / start_server 调用）：不触碰模型配置。

        None 跳过；真实变更即 detach 旧 harness（detach 语义同
        ``apply_model_config``）。
        """
        changed = False
        for attr, value in {
            "_workspace_dir": workspace_dir,
            "_dsh_home": dsh_home,
        }.items():
            if value is not None and value != getattr(self, attr):
                setattr(self, attr, value)
                changed = True
        if changed:
            self._detach_harness()

    def apply_model_config(self, config: DshModelConfig) -> None:
        """整组替换模型配置（detach 语义）：None 即显式清除该字段。

        provider/model 为 None 时归一化到默认值（未选模型路径），
        避免 provider 回落为空导致 harness 启动失败；真实变更即 detach
        旧 harness——无活动 turn 立即关闭，有则挂入待回收列表，由最后
        退出的生成器在 finally 中关闭；start 由 lifecycle 触发。
        """
        normalized = replace(
            config,
            provider=config.provider or _DEFAULT_PROVIDER,
            model=config.model or _DEFAULT_MODEL,
        )
        if normalized != self._model_config:
            self._model_config = normalized
            self._detach_harness()

    def reset_model_config(self) -> None:
        """将模型配置复位为默认值（切换 agent 时调用），防止上一 agent 的
        凭据被沿用；真实变更即 detach 旧 harness。"""
        self.apply_model_config(DshModelConfig())

    def ensure_harness(self) -> DeepSeekHarness:
        """按当前配置懒建 harness（不 start；start 由 lifecycle 触发）。

        持 ``_harness_lock`` 防并发 ensure 各建 harness，导致已 start 的子进程泄漏。
        非 deepseek 路由（``provider_route`` 非空）先物化 per-agent
        ``<dsh_home>/settings.yaml``，并把 api_key 经 ``api_key_env`` 注入子进程
        env；deepseek-official 走原生路径（api_key/base_url → DEEPSEEK_* env）。
        """
        with self._harness_lock:
            if self._harness is None:
                cfg = self._model_config
                env: dict[str, str] = {}
                api_key = cfg.api_key
                base_url = cfg.base_url
                if cfg.provider_route is not None:
                    if not self._dsh_home:
                        raise DshClientError(
                            reason="config-invalid",
                            message=(
                                "dsh provider route requires dsh_home "
                                "(agent workspace); cannot materialize "
                                "llm-pi-ai settings"
                            ),
                        )
                    self._materialize_pi_ai_settings(
                        Path(self._dsh_home), cfg.provider_route
                    )
                    api_key_env = cfg.provider_route.get("api_key_env")
                    if api_key_env and api_key:
                        env[api_key_env] = api_key
                    # 非 deepseek 路由不复用 DEEPSEEK_* env：凭据只经
                    # provider_route 的 api_key_env 注入，避免串扰。
                    api_key = None
                    base_url = None
                elif self._dsh_home:
                    # 路由切回 deepseek 原生路径：清除残留的 pi-ai 路由注册，
                    # 避免 settings.yaml 留下不再使用的 provider 条目。
                    self._clear_pi_ai_providers(Path(self._dsh_home))
                self._harness = DeepSeekHarness(
                    DeepSeekHarnessConfig(
                        cwd=self._workspace_dir,
                        dsh_home=self._dsh_home,
                        provider=cfg.provider,
                        model=cfg.model,
                        max_tokens=cfg.max_tokens,
                        api_key=api_key,
                        base_url=base_url,
                        env=env,
                    )
                )
            return self._harness

    def _materialize_pi_ai_settings(
        self, dsh_home: Path, route: dict[str, Any]
    ) -> None:
        """把当前 pi-ai provider 路由写入 ``<dsh_home>/settings.yaml``。

        dsh 启动时由 dsh-settings-file 插件读取（llm-pi-ai 插件据此注册
        provider 路由，initialize 的 provider 才能命中 adapter）。只替换
        ``llm-pi-ai.providers`` 子树，其余顶层配置节与同级键尽力保留；写失败
        抛 ``DshClientError``，不静默。
        """
        cfg = self._model_config
        profile: dict[str, Any] = {
            # 显式声明模型（覆盖/补充 catalog 条目）：注册表模型名不受 catalog
            # 目录收录与否限制。
            "models": [{"id": cfg.model, "name": cfg.model}]
        }
        api_key_env = route.get("api_key_env")
        if api_key_env and cfg.api_key:
            profile["apiKeyEnv"] = api_key_env
        if route.get("base_url"):
            profile["baseURL"] = route["base_url"]
        if route.get("api"):
            profile["api"] = route["api"]

        settings_path = dsh_home / "settings.yaml"
        document = self._load_settings_document(settings_path)
        pi_ai = document.get("llm-pi-ai")
        if not isinstance(pi_ai, dict):
            pi_ai = {}
        pi_ai["providers"] = {cfg.provider: profile}
        document["llm-pi-ai"] = pi_ai
        try:
            dsh_home.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                yaml.safe_dump(document, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            raise DshClientError(
                reason="config-invalid",
                message=f"dsh cannot write llm-pi-ai settings to {settings_path}: {exc}",
            ) from exc

    def _clear_pi_ai_providers(self, dsh_home: Path) -> None:
        """删除 ``<dsh_home>/settings.yaml`` 的 ``llm-pi-ai.providers`` 子树。

        路由切回 deepseek 原生路径时调用，清除残留的 provider 注册；文件
        不存在或无该子树时为 no-op。清理失败只告警不阻断（残留非致命）。
        """
        settings_path = dsh_home / "settings.yaml"
        if not settings_path.is_file():
            return
        document = self._load_settings_document(settings_path)
        pi_ai = document.get("llm-pi-ai")
        if not isinstance(pi_ai, dict) or "providers" not in pi_ai:
            return
        del pi_ai["providers"]
        try:
            settings_path.write_text(
                yaml.safe_dump(document, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            logger.warning(
                "dsh cannot clear stale llm-pi-ai providers in %s; leaving as-is",
                settings_path,
                exc_info=True,
            )

    @staticmethod
    def _load_settings_document(settings_path: Path) -> dict[str, Any]:
        """读取 settings.yaml 为 dict；缺失/不可读/非 dict 时告警并返回空文档。

        非 dict 的合法 YAML（如崩溃截断残留）同样告警，避免静默丢弃其它
        插件配置节。
        """
        if not settings_path.is_file():
            return {}
        try:
            loaded = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            logger.warning(
                "dsh cannot read existing settings.yaml at %s; overwriting",
                settings_path,
            )
            return {}
        if not isinstance(loaded, dict):
            logger.warning(
                "dsh settings.yaml at %s is not a mapping; overwriting",
                settings_path,
            )
            return {}
        return loaded

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
        self._session_map[session_key] = derive_dsh_session_id(session_key)

    def delete_session(self, *, session_key: str) -> None:
        """尽力而为：删 <dsh_home> 下该 session 的落盘文件；删除映射。"""
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
        session_id = derive_dsh_session_id(session_key)
        self._session_map[session_key] = session_id
        return session_id

    def _delete_session_files(self, dsh_session_id: str) -> None:
        """尽力而为删除 <dsh_home> 下该 session 的全部落盘文件。

        0.1.2rc1 真机落盘为嵌套布局：``<dsh_home>/sessions/<workspace-slug>/<sid>/...``
        与 ``<dsh_home>/storages/session_projcache/sessions/<sid>.json``。删除只扫描这两个
        已知落盘子树，按精确边界匹配（``name == sid`` 或 ``name.startswith(sid + ".")``）删
        除 session 目录/文件，。仅在本次删除确实清空了某目录时才向上清理空祖先；symlink 一律不跟随，
        避免越界删除 home 之外的目录/文件。``dsh_home`` 未配置时仅清映射（维持原语义）。
        """
        if not self._dsh_home:
            return
        home = Path(self._dsh_home)
        if not home.is_dir():
            return
        # 已知 session 落盘子树；protected 根（home 根 + 两个 session 根）永不清理。
        roots = (
            home / "sessions",
            home / "storages" / "session_projcache" / "sessions",
        )
        protected = {home, *roots}
        for root in roots:
            if root.is_dir() and not root.is_symlink():
                self._remove_session_entries(root, dsh_session_id, protected)

    def _remove_session_entries(
        self, root: Path, dsh_session_id: str, protected: set[Path]
    ) -> bool:
        """递归删除命中 ``dsh_session_id`` 的 session 落盘项（文件 / 整目录）。

        返回本子树是否删除了至少一项；仅当确实删除了内容、目录随之变空且不在
        ``protected`` 中时才 ``rmdir`` 它，避免误删与本次无关的既存空目录。
        """
        prefix = f"{dsh_session_id}."
        removed_any = False
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            logger.warning("dsh delete_session cannot list %s: %s", root, exc)
            return False
        for entry in entries:
            name = entry.name
            try:
                # 不跟随 symlink：命中名字的链接只删链接本身，绝不进入链接目标
                # （防越界删除 home 之外的目录/文件）。
                if entry.is_symlink():
                    if name == dsh_session_id or name.startswith(prefix):
                        entry.unlink()
                        removed_any = True
                    continue
                if entry.is_dir():
                    if name == dsh_session_id:
                        shutil.rmtree(entry)
                        removed_any = True
                        continue
                    if self._remove_session_entries(entry, dsh_session_id, protected):
                        removed_any = True
                elif name == dsh_session_id or name.startswith(prefix):
                    entry.unlink()
                    removed_any = True
            except OSError as exc:
                logger.warning(
                    "dsh delete_session best-effort removal failed for %s: %s",
                    entry,
                    exc,
                )
        if removed_any and root not in protected:
            try:
                if not any(root.iterdir()):
                    root.rmdir()
            except OSError:
                pass
        return removed_any


__all__ = [
    "DshClient",
    "DshClientError",
    "DshModelConfig",
    "derive_dsh_session_id",
]
