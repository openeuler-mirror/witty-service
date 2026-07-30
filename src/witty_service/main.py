import logging
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from witty_service.api.agents import router as agents_router
from witty_service.api.cve import router as cve_router
from witty_service.api.backport import router as backport_router
from witty_service.api.errors import register_exception_handlers
from witty_service.api.insight import router as insight_router
from witty_service.api.models import router as models_router
from witty_service.api.mcp_servers import router as mcp_servers_router
from witty_service.api.services import ServiceContainer, build_default_services
from witty_service.api.skills import router as skills_router
from witty_service.application.skill_manager import SkillManager
from witty_service.config import get_settings
from witty_service.logger import configure_logging

logger = logging.getLogger(__name__)


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
    def recover_agents() -> None:
        import asyncio
        from witty_service.application.agent_manager import _recovery_lock
        from witty_service.domain.enums import AgentStatus

        async def _recover_single_agent(agent, services, repository):
            """恢复单个 agent"""
            agent_id = agent.id
            try:
                logger.info("Recovering running agent: id=%s name=%s", agent_id, agent.name)
                agent_manager = services.get_agent_manager_for_agent(agent_id)
                await agent_manager.resume_agent(agent_id)
                logger.info("Successfully recovered running agent: id=%s", agent_id)
                return {"agent_id": agent_id, "success": True, "error": None}
            except Exception as exc:
                logger.error(
                    "Failed to recover running agent: id=%s error=%s",
                    agent_id,
                    exc,
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

        for sandbox_type in ("local_process", "docker"):
            # 使用 call_later 推迟到下一轮 event loop 迭代，
            # 避免 recovery 任务在 uvicorn 输出 "Application startup complete" 之前运行。
            asyncio.get_running_loop().call_later(
                0,
                lambda st=sandbox_type: asyncio.create_task(_recover_agents(st)),
            )

    @app.on_event("shutdown")
    async def close_services() -> None:
        await app.state.services.close()

    app.include_router(agents_router)
    app.include_router(cve_router)
    app.include_router(models_router)
    app.include_router(mcp_servers_router)
    app.include_router(skills_router)
    app.include_router(backport_router)
    app.include_router(insight_router)

    return app
