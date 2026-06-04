from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from witty_agent_server.application.composition.models import RuntimeInstance
from witty_agent_server.application.materialization.openclaw_materializer import (
    InvalidOpenClawSpecError,
    OpenClawMaterializationError,
    OpenClawSpecMaterializer,
    SpecNotFoundError,
)
from witty_agent_server.application.materialization.ports import (
    MaterializeReport,
    SpecMaterializerPort,
)
from witty_agent_server.application.materialization.core.io_utils import dump_json_atomic
from witty_agent_server.application.models.agent import Agent, AgentStatus
from witty_agent_server.application.models.agent_start import AgentStartRequest
from witty_agent_server.application.services.agent.base import AgentServiceBase
from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.application.services.agent.openclaw_lifecycle_service import (
    OpenClawLifecycleError,
    OpenClawLifecycleService,
)
from witty_agent_server.infra.ws.openclaw_gateway_client import OpenClawGatewayClient
from witty_agent_server.runtimes.runtime_base import RuntimeType

if TYPE_CHECKING:
    from witty_agent_server.application.composition.runtime_instance_manager import (
        RuntimeInstanceManager,
    )


logger = logging.getLogger(__name__)


class _DefaultOpenClawMaterializer(SpecMaterializerPort):
    """默认的 OpenClaw spec 物化器。"""

    def __init__(self) -> None:
        self._delegate = OpenClawSpecMaterializer()

    def materialize(
        self,
        spec_path: Path,
        *,
        output_path: Path | None = None,
        profile_name: str | None = None,
    ) -> MaterializeReport:
        return self._delegate.materialize(
            spec_path,
            output_path=output_path,
            profile_name=profile_name,
        )


class OpenClawAgentService(AgentServiceBase):
    """当前项目使用的 openclaw 版本 agent service。"""

    def __init__(
        self,
        agent: Agent | None = None,
        runtime_instance_manager: RuntimeInstanceManager | None = None,
        lifecycle_service: OpenClawLifecycleService | None = None,
        materializer: SpecMaterializerPort | None = None,
        gateway_agent_client: OpenClawGatewayClient | None = None,
        gateway_client_factory: Callable[..., OpenClawGatewayClient] = OpenClawGatewayClient,
        runtime: RuntimeType = "openclaw",
    ) -> None:
        super().__init__(agent=agent, runtime=runtime)
        self._runtime_instance_manager = runtime_instance_manager
        self._lifecycle_service = lifecycle_service or OpenClawLifecycleService()
        self._materializer = materializer or _DefaultOpenClawMaterializer()
        self._gateway_agent_client = gateway_agent_client or OpenClawGatewayClient()
        self._gateway_client_factory = gateway_client_factory

    def start(
        self,
        *,
        config: AgentStartRequest | None = None,
        reload: bool = False,
    ) -> Agent:
        """启动 openclaw runtime，并绑定到 gateway 中已加载的 agent。"""
        with self._lock:
            self._last_start_already_running = False
            requested_agent_id = config.agent_id if config is not None else None
            if not isinstance(requested_agent_id, str) or not requested_agent_id:
                raise AgentServiceError(
                    code="AGENT_ID_REQUIRED",
                    message="agent_id is required",
                    status_code=400,
                    details={"runtime_type": self._runtime},
                )
            if config is not None:
                self._agent.config = config.model_dump(
                    exclude_none=True,
                    exclude={"agent_id"},
                )
            # 解析完成后固化当前 agent 上下文，后续流程统一复用 self._agent.id。
            self._agent.id = requested_agent_id
            runtime_instance = self._prepare_runtime_instance()
            spec_path = self._require_instance_spec_path(runtime_instance)
            is_running = self._probe_openclaw_running(runtime_instance)
            logger.info(
                "agent start requested: agent_id=%s runtime=%s reload=%s running=%s profile=%s port=%s spec_path=%s",
                requested_agent_id,
                self._runtime,
                reload,
                is_running,
                runtime_instance.profile_name,
                runtime_instance.port,
                spec_path,
            )
            if isinstance(runtime_instance.config_path, Path) and runtime_instance.config_path.exists():
                self._sync_runtime_gateway_tokens(config_path=runtime_instance.config_path)
            self._probe_gateway_auth(runtime_instance)
            if is_running and not reload:
                self._backup_openclaw(runtime_instance)
                self._agent.status = AgentStatus.RUNNING
                self._last_start_already_running = True
                logger.info(
                    "agent start reused existing runtime: agent_id=%s runtime=%s",
                    requested_agent_id,
                    self._runtime,
                )
                return self.agent
            
            self._setup_openclaw(runtime_instance)
            self._materialize_spec(spec_path, runtime_instance=runtime_instance)
            if is_running:
                self._stop_openclaw(runtime_instance)
            self._start_openclaw(runtime_instance)
            if not self._probe_openclaw_running(runtime_instance):
                logger.warning(
                    "openclaw runtime probe failed after start: agent_id=%s runtime=%s",
                    requested_agent_id,
                    self._runtime,
                )
                raise AgentServiceError(
                    code="OPENCLAW_AGENT_INIT_FAILED",
                    message="openclaw agent init failed",
                    status_code=500,
                    details={"agent_id": requested_agent_id, "runtime_type": self._runtime},
                )

            self._backup_openclaw(runtime_instance)
            self._agent.status = AgentStatus.RUNNING
            logger.info(
                "agent start completed: agent_id=%s runtime=%s",
                requested_agent_id,
                self._runtime,
            )
            return self.agent

    def _prepare_runtime_instance(self) -> RuntimeInstance:
        """在启动前通过组合层显式准备 runtime instance。"""
        agent_id = self._agent.id
        if not isinstance(agent_id, str) or not agent_id:
            raise AgentServiceError(
                code="AGENT_ID_REQUIRED",
                message="agent_id is required",
                status_code=400,
                details={"runtime_type": self._runtime},
            )
        if self._runtime_instance_manager is None:
            raise AgentServiceError(
                code="RUNTIME_INSTANCE_MANAGER_REQUIRED",
                message="runtime instance manager is required",
                status_code=500,
                details={"agent_id": agent_id, "runtime_type": self._runtime},
            )

        from witty_agent_server.application.composition.models import (
            AgentBinding,
            RuntimeContextConfig,
            RuntimeTarget,
        )

        # config 中 witty-service 传入的 runtime_type / deployment_mode
        agent_config = self._agent.config or {}
        config_runtime_type = agent_config.get("runtime_type")
        if not isinstance(config_runtime_type, str) or not config_runtime_type:
            config_runtime_type = "openclaw"

        deployment_mode = agent_config.get("deployment_mode", "local")
        binding = AgentBinding(
            agent_id=agent_id,
            target=RuntimeTarget(
                runtime_type=cast(RuntimeType, config_runtime_type),
                deployment_mode=deployment_mode,  # type: ignore[arg-type]
            ),
            context=RuntimeContextConfig(),
        )
        logger.info(
            "_prepare_runtime_instance using config binding: agent_id=%s runtime=%s deployment=%s",
            agent_id,
            config_runtime_type,
            deployment_mode,
        )

        runtime_instance = self._runtime_instance_manager.ensure_instance(binding=binding)
        logger.info(
            "prepared runtime instance before agent start: agent_id=%s runtime=%s profile=%s port=%s",
            agent_id,
            self._runtime,
            runtime_instance.profile_name,
            runtime_instance.port,
        )
        return runtime_instance

    def _require_instance_spec_path(self, instance: RuntimeInstance) -> Path:
        """返回 runtime instance 对应的 agent spec 路径。"""
        spec_path = instance.spec_path
        if isinstance(spec_path, Path) and Path(spec_path).exists():
            return spec_path
        logger.error(
            "runtime instance spec path is missing: agent_id=%s runtime=%s",
            instance.agent_id,
            instance.runtime_type,
        )
        raise AgentServiceError(
            code="AGENT_SPEC_NOT_CONFIGURED",
            message="agent spec path is not configured",
            status_code=500,
            details={"agent_id": instance.agent_id, "runtime_type": instance.runtime_type},
        )

    def status(self, *, agent_id: str | None = None) -> Agent:
        with self._lock:
            self._ensure_agent_context(agent_id=agent_id)
            return self.agent

    def stop(self, *, agent_id: str | None = None) -> Agent:
        with self._lock:
            self._ensure_agent_context(agent_id=agent_id)
            self._transition(
                allowed_current=(AgentStatus.RUNNING, AgentStatus.PAUSED),
                target=AgentStatus.STOPPED,
            )
            return self.agent

    def list_agents(self) -> dict[str, Any]:
        """返回 gateway 可见 agent 列表。"""
        return self._gateway_agent_client.list_agents()

    def resolve_default_agent(self) -> str:
        """解析默认 agent id。"""
        payload = self._gateway_agent_client.list_agents()
        default_id = payload.get("defaultId")
        if isinstance(default_id, str) and default_id:
            return default_id
        configured_agents = payload.get("agents")
        if isinstance(configured_agents, list):
            for item in configured_agents:
                if isinstance(item, dict) and item.get("default") is True:
                    raw_id = item.get("id")
                    if isinstance(raw_id, str) and raw_id:
                        return raw_id
        raise AgentServiceError(
            code="AGENT_DEFAULT_NOT_CONFIGURED",
            message="default agent is not configured",
            status_code=400,
            details=None,
        )

    def _probe_openclaw_running(self, instance: RuntimeInstance) -> bool:
        """探测 openclaw gateway/runtime 当前是否已就绪。"""
        try:
            return self._lifecycle_service.probe_instance(instance)
        except OpenClawLifecycleError as exc:
            raise AgentServiceError(
                code="OPENCLAW_START_FAILED",
                message="openclaw start failed",
                status_code=500,
                details=self._lifecycle_error_details(exc),
            ) from exc

    def _setup_openclaw(self, instance: RuntimeInstance) -> None:
        """执行 openclaw profile 级 setup。"""
        try:
            self._lifecycle_service.setup_instance(instance)
        except OpenClawLifecycleError as exc:
            raise AgentServiceError(
                code="OPENCLAW_SETUP_FAILED",
                message="openclaw setup failed",
                status_code=500,
                details=self._lifecycle_error_details(exc),
            ) from exc

    def _materialize_spec(self, spec_path: Path, *, runtime_instance: RuntimeInstance) -> None:
        """将 agent spec 物化到当前 runtime instance 对应的配置目录。"""
        config_path = runtime_instance.config_path
        profile_name = runtime_instance.profile_name
        if not isinstance(config_path, Path):
            raise AgentServiceError(
                code="RUNTIME_CONFIG_PATH_REQUIRED",
                message="runtime config path is required",
                status_code=500,
                details={"agent_id": runtime_instance.agent_id, "runtime_type": runtime_instance.runtime_type},
            )
        try:
            logger.info(
                "materialize agent spec: agent_id=%s runtime=%s profile=%s output_path=%s spec_path=%s",
                runtime_instance.agent_id,
                runtime_instance.runtime_type,
                profile_name,
                config_path,
                spec_path,
            )
            self._materializer.materialize(
                spec_path,
                output_path=config_path,
                profile_name=profile_name,
            )
            self._write_runtime_gateway_port(config_path=config_path, port=runtime_instance.port)
            self._sync_runtime_gateway_tokens(config_path=config_path)
        except SpecNotFoundError as exc:
            raise AgentServiceError(
                code="AGENT_SPEC_NOT_FOUND",
                message="agent spec not found",
                status_code=400,
                details={"spec_path": str(exc.spec_path)},
            ) from exc
        except InvalidOpenClawSpecError as exc:
            raise AgentServiceError(
                code="AGENT_SPEC_INVALID",
                message="agent spec is invalid",
                status_code=400,
                details={"spec_path": str(exc.spec_path)},
            ) from exc
        except OpenClawMaterializationError as exc:
            raise AgentServiceError(
                code="AGENT_SPEC_MATERIALIZE_FAILED",
                message="agent spec materialization failed",
                status_code=500,
                details={"spec_path": str(exc.spec_path), "error": str(exc)},
            ) from exc
        except Exception as exc:
            raise AgentServiceError(
                code="AGENT_SPEC_MATERIALIZE_FAILED",
                message="agent spec materialization failed",
                status_code=500,
                details={"spec_path": str(spec_path)},
            ) from exc

    def _write_runtime_gateway_port(self, *, config_path: Path, port: int | None) -> None:
        """把当前实例端口写回 profile 配置。"""
        if not isinstance(port, int):
            raise AgentServiceError(
                code="RUNTIME_PORT_REQUIRED",
                message="runtime port is required",
                status_code=500,
                details={"config_path": str(config_path)},
            )
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("openclaw config must be a JSON object")
            gateway = payload.get("gateway")
            if not isinstance(gateway, dict):
                gateway = {}
                payload["gateway"] = gateway
            gateway["port"] = port
            dump_json_atomic(str(config_path), payload)
            logger.info("synced runtime gateway port: config_path=%s port=%s", config_path, port)
        except Exception as exc:
            raise AgentServiceError(
                code="OPENCLAW_CONFIG_SYNC_FAILED",
                message="openclaw config sync failed",
                status_code=500,
                details={"config_path": str(config_path), "port": port},
            ) from exc

    def _sync_runtime_gateway_tokens(self, *, config_path: Path) -> None:
        """同步 gateway.auth.token 与 gateway.remote.token，避免首次握手因配对漂移失败。"""
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("openclaw config must be a JSON object")
            gateway = payload.get("gateway")
            if not isinstance(gateway, dict):
                return
            auth = gateway.get("auth")
            if not isinstance(auth, dict):
                return
            auth_token = auth.get("token")
            if not isinstance(auth_token, str) or not auth_token:
                return
            remote = gateway.get("remote")
            if not isinstance(remote, dict):
                remote = {}
                gateway["remote"] = remote
            remote_token = remote.get("token")
            if remote_token != auth_token:
                remote["token"] = auth_token
                dump_json_atomic(str(config_path), payload)
                logger.info(
                    "synced runtime gateway tokens: config_path=%s remote_token_updated=True",
                    config_path,
                )
        except Exception as exc:
            raise AgentServiceError(
                code="OPENCLAW_CONFIG_SYNC_FAILED",
                message="openclaw config sync failed",
                status_code=500,
                details={"config_path": str(config_path)},
            ) from exc

    def _probe_gateway_auth(self, instance: RuntimeInstance) -> None:
        """用真实 connect 握手做一次鉴权预检，避免问题延后到会话流量阶段。"""
        gateway_client = self._gateway_client_factory(
            url=f"ws://127.0.0.1:{instance.port}",
            token=None,
            state_dir=instance.state_dir,
        )
        try:
            gateway_client.probe_gateway_auth()
        except Exception as exc:
            code = getattr(exc, "code", "GATEWAY_AUTH_FAILED")
            message = getattr(exc, "message", "gateway auth probe failed")
            raise AgentServiceError(
                code=code if isinstance(code, str) and code else "GATEWAY_AUTH_FAILED",
                message=message if isinstance(message, str) and message else "gateway auth probe failed",
                status_code=500,
                details={"runtime_type": self._runtime},
            ) from exc

    def _stop_openclaw(self, instance: RuntimeInstance) -> None:
        """重载前先停止旧 runtime，避免进程和端口残留。"""
        try:
            self._lifecycle_service.stop_instance(instance)
        except OpenClawLifecycleError as exc:
            raise AgentServiceError(
                code="OPENCLAW_STOP_FAILED",
                message="openclaw stop failed",
                status_code=500,
                details=self._lifecycle_error_details(exc),
            ) from exc

    def _start_openclaw(self, instance: RuntimeInstance) -> None:
        """启动 openclaw runtime。"""
        try:
            self._lifecycle_service.install_instance(instance, force=True)
            self._lifecycle_service.start_instance(instance)
        except OpenClawLifecycleError as exc:
            raise AgentServiceError(
                code="OPENCLAW_START_FAILED",
                message="openclaw start failed",
                status_code=500,
                details=self._lifecycle_error_details(exc),
            ) from exc

    def _backup_openclaw(self, instance: RuntimeInstance) -> None:
        """将当前 openclaw profile home 备份到 runtime_backup_dir。"""
        backup_instance = getattr(self._lifecycle_service, "backup_instance", None)
        if backup_instance is None:
            logger.debug(
                "openclaw lifecycle service does not support backup: agent_id=%s runtime=%s",
                instance.agent_id,
                instance.runtime_type,
            )
            return
        try:
            backup_instance(instance)
        except Exception as exc:
            logger.warning(
                "openclaw backup failed: agent_id=%s runtime=%s error=%s",
                instance.agent_id,
                instance.runtime_type,
                exc,
            )

    def _lifecycle_error_details(
        self,
        exc: OpenClawLifecycleError,
    ) -> dict[str, Any]:
        """将 lifecycle 错误标准化为响应 details。"""
        return {
            "action": exc.action,
            "command": list(exc.command),
            "returncode": exc.returncode,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        }
