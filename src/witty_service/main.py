import inspect
import logging
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from witty_service.api.agent_templates import router as agent_templates_router
from witty_service.api.agents import router as agents_router
from witty_service.api.backport import router as backport_router
from witty_service.api.channels import router as channels_router
from witty_service.api.cve import router as cve_router
from witty_service.api.errors import register_exception_handlers
from witty_service.api.insight import router as insight_router
from witty_service.api.mcp_servers import router as mcp_servers_router
from witty_service.api.models import router as models_router
from witty_service.api.scheduled_tasks import router as scheduled_tasks_router
from witty_service.api.services import ServiceContainer, build_default_services
from witty_service.api.skills import router as skills_router
from witty_service.application.skill_manager import SkillManager
from witty_service.config import get_settings
from witty_service.logger import configure_logging

logger = logging.getLogger(__name__)

#: wal_checkpoint 周期：1 小时（保证 ``-wal`` 无上限增长，不频繁到影响性能）
_CHECKPOINT_INTERVAL_S = 3600.0


async def _call(method: object) -> Any:
    """调用容器成员并兼容**非协程测试替身**（`MagicMock` 容器的 `start`/`stop`）。

    生产路径上这些成员都是协程函数，因此这里只是让既有的 `MagicMock()` 容器测试
    （见实施计划 §4.2）不需要为渠道能力改写替身，行为不变。
    """
    if not callable(method):
        return None
    result = method()
    if inspect.isawaitable(result):
        return await result
    return result


def create_app(*, services: ServiceContainer | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(title="Witty Service")
    app.state.services = services or build_default_services()
    register_exception_handlers(app)

    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.origins,
        allow_credentials=settings.cors.credentials,
        allow_methods=settings.cors.methods,
        allow_headers=settings.cors.headers,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/server/capabilities")
    def capabilities() -> dict[str, list[str]]:
        return {"supported_runtimes": list(settings.runtime.supported)}

    @app.on_event("startup")
    def sync_awesome_openclaw_skills_on_startup() -> None:
        threading.Thread(
            target=SkillManager.sync_awesome_repository_in_background,
            kwargs={"repository": app.state.services.repository},
            daemon=True,
        ).start()

    @app.on_event("startup")
    def prewarm_skill_repos_on_startup() -> None:
        """后台预热预置模板的 skill 内容仓库缓存（B2）。

        只做缓存填充，失败仅记 warning，绝不阻塞/中断启动；实例化时的懒兜底保证可用。
        """
        from witty_service.application.agent_template_service import (
            AgentTemplateService,
        )

        services = app.state.services

        def _prewarm() -> None:
            try:
                template_service = AgentTemplateService(
                    repository=services.repository,
                    agent_manager_factory=services.get_agent_manager_for_sandbox,
                )
                template_service.prewarm_skill_repos()
            except Exception:
                logger.exception("Failed to prewarm skill repos on startup")

        threading.Thread(target=_prewarm, daemon=True).start()

    @app.on_event("startup")
    def recover_stale_generations() -> None:
        from witty_service.persistence.orm import MessageStatus

        repository = app.state.services.repository
        stale = repository.find_stale_generating_messages(stale_threshold_seconds=30)
        for msg in stale:
            try:
                repository.update_message_status(msg.id, MessageStatus.interrupted)
                logger.info("Recovered stale generating message: %s", msg.id)
            except Exception:
                logger.warning(
                    "Failed to recover stale message: %s", msg.id, exc_info=True
                )

    @app.on_event("startup")
    def recover_backport_runs() -> None:
        from witty_service.application.backport_run_store import BackportRunStore

        base_dir = app.state.services.workspace_store.base_dir
        app.state.backport_run_store = BackportRunStore(base_dir / "backport-runs")
        app.state.backport_run_store.list_runs(active_run_ids=set())

    @app.on_event("startup")
    def reap_orphan_agent_servers() -> None:
        """收敛游离的 witty-agent-server 进程树（服务重启/手动测试遗留）。

        必须先于 recover_agents 执行：孤儿进程占着端口与内存，还会干扰恢复时的
        端口复用判定
        """
        from pathlib import Path

        from witty_service.sandbox.local_process import LocalProcessSandboxBackend

        services = app.state.services
        backend = services.get_sandbox_backend("local_process")
        if not isinstance(backend, LocalProcessSandboxBackend):
            return
        repository = services.repository
        registered: dict[int, str | None] = {}
        for agent in repository.list_agents():
            state = repository.get_sandbox_state(agent.id)
            if state is None:
                continue
            metadata = state.sandbox_payload_json.get("metadata", {})
            pid = metadata.get("pid")
            start_time = metadata.get("process_start_time")
            if isinstance(pid, int):
                registered[pid] = start_time if isinstance(start_time, str) else None
        try:
            reaped = backend.reap_orphan_agent_servers(
                registered=registered,
                workspace_root=Path(services.workspace_store.base_dir),
            )
        except Exception:
            logger.exception("Failed to reap orphan agent servers on startup")
            return
        if reaped:
            logger.warning("Reaped %d orphan agent server(s): %s", len(reaped), reaped)

    @app.on_event("startup")
    async def recover_agents() -> None:
        import asyncio

        from witty_service.application.agent_manager import _recovery_lock
        from witty_service.domain.enums import AgentStatus

        async def _recover_single_agent(agent, services, repository):
            """恢复单个 agent"""
            agent_id = agent.id
            try:
                logger.info(
                    "Recovering running agent: id=%s name=%s", agent_id, agent.name
                )
                agent_manager = services.get_agent_manager_for_agent(agent_id)
                await agent_manager.resume_agent(agent_id)
                logger.info("Successfully recovered running agent: id=%s", agent_id)
                return {"agent_id": agent_id, "success": True, "error": None}
            except Exception as exc:
                # DomainError.__str__ 只有 message；根因（上游 code / 响应体）在 details 里。
                logger.error(
                    "Failed to recover running agent: id=%s error=%s details=%s",
                    agent_id,
                    exc,
                    getattr(exc, "details", None),
                    exc_info=True,
                )
                try:
                    repository.update_agent_status(agent_id, AgentStatus.error)
                except Exception:
                    logger.warning(
                        "Failed to update agent status to error: id=%s",
                        agent_id,
                        exc_info=True,
                    )
                return {"agent_id": agent_id, "success": False, "error": str(exc)}

        async def _recover_agents(sandbox_type: str):
            services = app.state.services
            repository = services.repository

            agents_needing_recovery = repository.list_agents_needing_recovery(
                sandbox_type=sandbox_type,
                status_filter=[AgentStatus.running],
            )
            if not agents_needing_recovery:
                logger.info("No running %s agents need recovery", sandbox_type)
                return

            agent_count = len(agents_needing_recovery)
            logger.info(
                "Found %d running %s agent(s) needing recovery",
                agent_count,
                sandbox_type,
            )

            async with _recovery_lock:
                logger.info("Acquired recovery lock, starting recovery...")
                results = []
                for agent in agents_needing_recovery:
                    result = await _recover_single_agent(agent, services, repository)
                    results.append(result)

                success_count = sum(1 for r in results if r["success"])
                fail_count = agent_count - success_count
                logger.info(
                    "Recovery completed: %d succeeded, %d failed",
                    success_count,
                    fail_count,
                )
                logger.info("Application startup complete")
            logger.info("Released recovery lock")

        # 先完成 agent 恢复，再让后续 startup handler 启动定时任务调度器，
        # 避免定时任务在 agent 恢复窗口内触发而报 TASK_AGENT_NOT_RUNNABLE。
        try:
            await asyncio.gather(
                _recover_agents("local_process"),
                _recover_agents("docker"),
            )
        except Exception:
            logger.exception("Agent recovery failed during startup")

    @app.on_event("startup")
    async def start_scheduled_tasks() -> None:
        """启动定时任务调度器：agent 恢复完成后再注册任务，避免恢复窗口内的触发失败。"""
        await app.state.services.scheduled_task_service.start()

    @app.on_event("startup")
    async def start_channel_gateway() -> None:
        """启动渠道网关（框架设计 §7.2）。

        挂载点必须在 agent 恢复与定时任务之后，否则恢复期内收到的消息会打到未就绪的
        agent。网关自身的守卫拒绝（进程数不为 1 / 密钥非法）会返回 False，**不抛异常**；
        意外异常也只记日志——渠道是增量能力，不能让它拖垮既有接口。
        """
        gateway = getattr(app.state.services, "channel_gateway", None)
        if gateway is None:
            return
        try:
            started = bool(await _call(gateway.start))
        except Exception:
            logger.exception("Channel gateway failed to start")
            return
        if not started:
            logger.warning(
                "Channel gateway did not start; no channel connection is established "
                "(reason=%s)",
                getattr(gateway, "guard_reason", None),
            )

    @app.on_event("startup")
    def start_wal_checkpoint_scheduler() -> None:
        """每小时对 sqlite 主库做一次 wal_checkpoint(TRUNCATE)。

        WAL 模式下写入只追加 ``-wal`` 文件；若存在长期持有旧读快照的连接，
        autocheckpoint 推进不过去，``-wal`` 会无上限增长（磁盘泄漏 + 重启变慢）。
        低频 checkpoint 把它截断回 0。详见 db.checkpoint_database 的注释——
        切忌高频调用，会退化回"每次 commit 都 fsync"。
        """
        from witty_service.persistence.db import checkpoint_database

        _stop_event = threading.Event()

        def _run_checkpoint_forever() -> None:
            while not _stop_event.wait(_CHECKPOINT_INTERVAL_S):
                try:
                    checkpoint_database()
                except Exception:
                    logger.exception("Periodic wal_checkpoint failed")

        threading.Thread(
            target=_run_checkpoint_forever,
            name="wal-checkpoint",
            daemon=True,
        ).start()
        app.state.wal_checkpoint_stop_event = _stop_event

    @app.on_event("shutdown")
    async def close_services() -> None:
        # 关闭顺序（框架设计 §7.2）：网关 -> 调度器 -> services.close()
        checkpoint_stop_event = getattr(app.state, "wal_checkpoint_stop_event", None)
        if checkpoint_stop_event is not None:
            checkpoint_stop_event.set()
        gateway = getattr(app.state.services, "channel_gateway", None)
        if gateway is not None:
            await _call(gateway.stop)
        await app.state.services.scheduled_task_service.shutdown()
        await app.state.services.close()

    app.include_router(agents_router)
    app.include_router(channels_router)
    app.include_router(agent_templates_router)
    app.include_router(cve_router)
    app.include_router(models_router)
    app.include_router(mcp_servers_router)
    app.include_router(scheduled_tasks_router)
    app.include_router(skills_router)
    app.include_router(backport_router)
    app.include_router(insight_router)

    return app
