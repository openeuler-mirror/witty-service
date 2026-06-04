from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from threading import RLock

from witty_agent_server.application.composition.models import AgentBinding, RuntimeInstance
from witty_agent_server.application.materialization.openclaw_paths import (
    resolve_openclaw_home_dir,
)
from witty_agent_server.runtimes.runtime_base import RuntimeType


logger = logging.getLogger(__name__)


class RuntimeInstanceManager:
    """管理 local/sandbox 场景下的 runtime instance。"""

    def __init__(self, *, base_root: Path | str) -> None:
        resolved_base_root = (
            base_root.expanduser() if isinstance(base_root, Path) else Path(base_root).expanduser()
        )
        self._base_root = resolved_base_root
        self._instances: dict[str, RuntimeInstance] = {}
        self._lock = RLock()

    def ensure_instance(self, *, binding: AgentBinding) -> RuntimeInstance:
        """确保指定 binding 对应的 runtime instance 已创建。"""
        instance_key = self._build_instance_key(binding.agent_id)
        with self._lock:
            existing_instance = self._instances.get(instance_key)
            if existing_instance is not None:
                logger.info(
                    "reused runtime instance: agent_id=%s runtime=%s profile=%s port=%s",
                    binding.agent_id,
                    binding.target.runtime_type,
                    existing_instance.profile_name,
                    existing_instance.port,
                )
                return existing_instance

            instance = self._create_instance(binding=binding, instance_key=instance_key)
            self._instances[instance_key] = instance
            logger.info(
                "created runtime instance: agent_id=%s runtime=%s profile=%s port=%s",
                binding.agent_id,
                binding.target.runtime_type,
                instance.profile_name,
                instance.port,
            )
            return instance

    def get_instance(self, *, binding: AgentBinding) -> RuntimeInstance | None:
        """返回指定 binding 对应的 instance。"""
        with self._lock:
            return self._instances.get(self._build_instance_key(binding.agent_id))

    def get_instance_by_agent_id(
        self,
        *,
        agent_id: str,
        runtime_candidates: tuple[RuntimeType, ...] | None = None,
    ) -> RuntimeInstance | None:
        """按 agent_id 返回已缓存或落盘的实例元数据。"""
        instance_key = self._build_instance_key(agent_id)
        with self._lock:
            instance = self._instances.get(instance_key)
        if instance is not None:
            return instance

        candidates = runtime_candidates or ()
        for runtime_type in candidates:
            metadata_path = self._base_root / f"{runtime_type}_{agent_id}" / "instance.json"
            if not metadata_path.exists():
                continue
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                resolved = RuntimeInstance.model_validate(payload)
            except Exception as exc:
                logger.warning(
                    "failed to load runtime instance metadata: agent_id=%s runtime_type=%s path=%s error=%s",
                    agent_id,
                    runtime_type,
                    metadata_path,
                    exc,
                )
                continue
            with self._lock:
                self._instances[instance_key] = resolved
            logger.info(
                "loaded runtime instance from metadata: agent_id=%s runtime_type=%s path=%s",
                agent_id,
                runtime_type,
                metadata_path,
            )
            return resolved
        return None

    def release_instance(self, *, binding: AgentBinding) -> RuntimeInstance | None:
        """释放指定 binding 对应的缓存 instance。"""
        instance_key = self._build_instance_key(binding.agent_id)
        with self._lock:
            removed = self._instances.pop(instance_key, None)
        if removed is not None:
            logger.info(
                "released runtime instance: agent_id=%s runtime=%s",
                binding.agent_id,
                binding.target.runtime_type,
            )
        return removed

    def status(self, *, binding: AgentBinding) -> str | None:
        """返回 instance 最小状态。"""
        instance = self.get_instance(binding=binding)
        if instance is None:
            return None
        return "ready"

    def get_runtime_type(
        self,
        *,
        agent_id: str,
        runtime_candidates: tuple[RuntimeType, ...] | None = None,
    ) -> str | None:
        """根据 agent_id 查询 runtime_type，优先内存缓存，未命中按候选 runtime 精确查 instance.json。"""
        instance_key = self._build_instance_key(agent_id)
        with self._lock:
            instance = self._instances.get(instance_key)
        if instance is None:
            return self._read_runtime_type_from_metadata(
                agent_id=agent_id,
                runtime_candidates=runtime_candidates,
            )
        return instance.runtime_type

    def _read_runtime_type_from_metadata(
        self,
        *,
        agent_id: str,
        runtime_candidates: tuple[RuntimeType, ...] | None = None,
    ) -> RuntimeType | None:
        """按 {runtime_type}_{agent_id}/instance.json 规则精确读取 runtime_type。"""
        candidates = runtime_candidates or ()
        for runtime_type in candidates:
            agent_root = self._base_root / f"{runtime_type}_{agent_id}"
            metadata_path = agent_root / "instance.json"
            if not metadata_path.exists():
                continue
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "failed to read instance metadata: agent_id=%s runtime_type=%s path=%s error=%s",
                    agent_id,
                    runtime_type,
                    metadata_path,
                    exc,
                )
                continue
            resolved_runtime_type = payload.get("runtime_type")
            if isinstance(resolved_runtime_type, str) and resolved_runtime_type:
                logger.info(
                    "resolved runtime_type from instance metadata: agent_id=%s runtime_type=%s path=%s",
                    agent_id,
                    resolved_runtime_type,
                    metadata_path,
                )
                return resolved_runtime_type
            logger.warning(
                "invalid runtime_type in instance metadata: agent_id=%s runtime_type=%s path=%s",
                agent_id,
                runtime_type,
                metadata_path,
            )
        logger.warning(
            "instance metadata not found for agent_id=%s under configured runtimes=%s",
            agent_id,
            list(candidates),
        )
        return None

    def _create_instance(
        self,
        *,
        binding: AgentBinding,
        instance_key: str,
    ) -> RuntimeInstance:
        """创建并持久化新的 runtime instance。"""
        if binding.target.deployment_mode == "sandbox":
            return self._create_sandbox_instance(binding=binding, instance_key=instance_key)
        return self._create_local_instance(binding=binding, instance_key=instance_key)

    def _create_local_instance(
        self,
        *,
        binding: AgentBinding,
        instance_key: str,
    ) -> RuntimeInstance:
        agent_root = self._base_root / f"{binding.target.runtime_type}_{binding.agent_id}"
        profile_name = self._build_profile_name(agent_id=binding.agent_id)
        state_dir = self._build_state_dir(
            runtime_type=binding.target.runtime_type,
            agent_root=agent_root,
            profile_name=profile_name,
        )
        config_path = self._build_config_path(
            runtime_type=binding.target.runtime_type,
            state_dir=state_dir,
        )
        port = self._build_instance_port(
            agent_id=binding.agent_id,
            runtime_type=binding.target.runtime_type,
        )
        spec_path = agent_root / "agent-config" / "agent-spec.yaml"
        runtime_backup_dir = self._build_backup_dir(
            runtime_type=binding.target.runtime_type,
            agent_root=agent_root,
        )
        workspace_dir = state_dir / "workspace"
        log_dir = agent_root / "logs"

        # agent-config/ 由 witty-service 创建，此处不 mkdir
        # runtime home/workspace、备份目录和日志目录由 witty-agent-server 创建。
        for path in (workspace_dir, runtime_backup_dir, log_dir):
            path.mkdir(parents=True, exist_ok=True)

        instance = RuntimeInstance(
            agent_id=binding.agent_id,
            runtime_type=binding.target.runtime_type,
            deployment_mode="local",
            instance_key=instance_key,
            profile_name=profile_name,
            state_dir=state_dir,
            runtime_backup_dir=runtime_backup_dir,
            config_path=config_path,
            workspace_dir=workspace_dir,
            log_dir=log_dir,
            spec_path=spec_path,
            port=port,
        )
        self._write_instance_metadata(agent_root=agent_root, instance=instance)
        return instance

    def _create_sandbox_instance(
        self,
        *,
        binding: AgentBinding,
        instance_key: str,
    ) -> RuntimeInstance:
        agent_root = self._base_root / f"{binding.target.runtime_type}_{binding.agent_id}"
        profile_name = self._build_profile_name(agent_id=binding.agent_id)
        state_dir = self._build_state_dir(
            runtime_type=binding.target.runtime_type,
            agent_root=agent_root,
            profile_name=profile_name,
        )
        config_path = self._build_config_path(
            runtime_type=binding.target.runtime_type,
            state_dir=state_dir,
        )
        spec_path = agent_root / "agent-config" / "agent-spec.yaml"
        runtime_backup_dir = self._build_backup_dir(
            runtime_type=binding.target.runtime_type,
            agent_root=agent_root,
        )
        workspace_dir = state_dir / "workspace"
        log_dir = agent_root / "logs"

        # runtime home/workspace、备份目录和日志目录由 witty-agent-server 创建。
        for path in (workspace_dir, runtime_backup_dir, log_dir):
            path.mkdir(parents=True, exist_ok=True)

        instance = RuntimeInstance(
            agent_id=binding.agent_id,
            runtime_type=binding.target.runtime_type,
            deployment_mode="sandbox",
            instance_key=instance_key,
            sandbox_id=binding.target.sandbox_id,
            profile_name=profile_name,
            state_dir=state_dir,
            runtime_backup_dir=runtime_backup_dir,
            config_path=config_path,
            workspace_dir=workspace_dir,
            log_dir=log_dir,
            spec_path=spec_path,
        )
        self._write_instance_metadata(agent_root=agent_root, instance=instance)
        return instance

    def _build_instance_key(self, agent_id: str) -> str:
        """构造实例键：直接复用 agent_id。"""
        return agent_id

    def _build_profile_name(self, *, agent_id: str) -> str:
        """构造 runtime profile 名称：witty-{agent_id}。"""
        return f"witty-{agent_id}"

    def _build_state_dir(
        self,
        *,
        runtime_type: str,
        agent_root: Path,
        profile_name: str | None = None,
    ) -> Path:
        """构造 runtime config home 目录。"""
        if runtime_type == "openclaw":
            return resolve_openclaw_home_dir(profile_name=profile_name)
        return agent_root / f"{runtime_type}-config-home"

    def _build_backup_dir(
        self,
        *,
        runtime_type: str,
        agent_root: Path,
    ) -> Path:
        """构造 runtime 备份目录，默认落在 agent_root 下的 openclaw-config-home。"""
        return agent_root / f"{runtime_type}-config-home"

    def _build_config_path(
        self,
        *,
        runtime_type: str,
        state_dir: Path,
    ) -> Path:
        """构造 runtime 配置文件路径。"""
        if runtime_type == "openclaw":
            return state_dir / "openclaw.json"
        if runtime_type == "opencode":
            return state_dir / "opencode.json"
        return state_dir / "config.json"

    def _build_instance_port(self, *, agent_id: str, runtime_type: str) -> int:
        """基于 agent 和 runtime 生成稳定端口。"""
        digest = hashlib.sha256(f"{runtime_type}:{agent_id}".encode("utf-8")).hexdigest()
        return 18000 + (int(digest[:6], 16) % 10000)

    def _write_instance_metadata(
        self,
        *,
        agent_root: Path,
        instance: RuntimeInstance,
    ) -> None:
        """将实例元数据写入 agent home 下 instance.json。"""
        payload = instance.model_dump(mode="json")
        metadata_path = agent_root / "instance.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "wrote runtime instance metadata: agent_id=%s metadata_path=%s",
            instance.agent_id,
            metadata_path,
        )
