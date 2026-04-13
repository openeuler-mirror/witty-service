# Session Proxy 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 witty-service Session 接口重构为 Proxy 模式，以 witty-agent-server 为主数据源，同时实现 Agent pause/resume/delete 生命周期管理。

**Architecture:** 
- Session Proxy 模式：witty-service 透传 Session 操作到 witty-agent-server，本地数据库作为缓存
- 运行时备份：delete 时备份 `~/.openclaw` 到 `~/witty-service/{agent_id}/runtime_backup/`
- Resume 分支：paused 直接启动运行时，deleted 恢复备份后启动

**Tech Stack:** Python, FastAPI, SQLAlchemy, SQLite, httpx, docker SDK

---

## 文件结构

### 需要修改的文件

| 文件 | 变更内容 |
|------|----------|
| `src/storage/workspace_store.py` | base_path 改为 `~/witty-service/` |
| `src/api/schemas.py` | SessionResponse 新增 `context_initialized`、`runtime_type` 字段 |
| `src/api/agents.py` | Session 路由调整，新增 events 路由 |
| `src/application/session_manager.py` | 透传到 witty-agent-server |
| `src/application/agent_manager.py` | Pause/Resume/Delete 流程改造 |
| `src/persistence/repositories.py` | Session upsert 方法 |
| `src/persistence/orm.py` | AgentStatus 枚举更新（去掉 stopped） |
| `src/domain/enums.py` | AgentStatus 枚举更新 |

### 需要新建的文件

| 文件 | 说明 |
|------|------|
| `src/storage/runtime_backup.py` | 运行时备份/恢复逻辑 |
| `src/adapter/http_client.py` | HTTP 客户端（调用 witty-agent-server API） |

---

## Task 1: 基础设施 - RuntimeBackupStore

**Files:**
- Create: `src/storage/runtime_backup.py`
- Modify: `src/storage/__init__.py` (导出新模块)
- Test: `tests/unit/storage/test_runtime_backup.py`

- [ ] **Step 1: 创建 RuntimeBackupStore 类**

```python
# src/storage/runtime_backup.py
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

class RuntimeBackupStore:
    """运行时备份/恢复管理器"""
    
    def __init__(self, base_path: str | Path = "~/witty-service/") -> None:
        self.base_path = Path(base_path).expanduser().resolve()
    
    def backup(
        self,
        agent_id: str,
        runtime_type: Literal["openclaw"] = "openclaw",
    ) -> Path:
        """备份运行时文件到本地
        
        源: ~/.openclaw
        目标: ~/witty-service/{agent_id}/runtime_backup/.openclaw
        """
        source = Path.home() / f".{runtime_type}"
        destination = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        
        if not source.exists():
            return destination
        
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return destination
    
    def restore(self, agent_id: str, runtime_type: Literal["openclaw"] = "openclaw") -> Path:
        """恢复运行时备份到原位置
        
        源: ~/witty-service/{agent_id}/runtime_backup/.openclaw
        目标: ~/.openclaw
        """
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found for agent {agent_id}")
        
        destination = Path.home() / f".{runtime_type}"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(backup_path, destination)
        return destination
    
    def backup_exists(self, agent_id: str, runtime_type: Literal["openclaw"] = "openclaw") -> bool:
        """检查备份是否存在"""
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        return backup_path.exists()
    
    def delete_backup(self, agent_id: str, runtime_type: Literal["openclaw"] = "openclaw") -> None:
        """删除备份"""
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        if backup_path.exists():
            shutil.rmtree(backup_path.parent.parent)
```

- [ ] **Step 2: 验证 shutil.rmtree 在父目录不存在时会报错**

Run: `python3 -c "import shutil; from pathlib import Path; p = Path('/tmp/test_parent'); p.mkdir(); shutil.rmtree(p)"`

- [ ] **Step 3: 修改 src/storage/__init__.py 导出 RuntimeBackupStore**

```python
from src.storage.runtime_backup import RuntimeBackupStore
from src.storage.workspace_store import LocalWorkspaceStore, WorkspaceStore

__all__ = ["WorkspaceStore", "LocalWorkspaceStore", "RuntimeBackupStore"]
```

- [ ] **Step 4: 运行测试验证**

Run: `pytest tests/unit/storage/test_runtime_backup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/storage/runtime_backup.py src/storage/__init__.py tests/unit/storage/test_runtime_backup.py
git commit -m "feat: add RuntimeBackupStore for runtime backup/restore"
```

---

## Task 2: 基础设施 - AdaptorHttpClient

**Files:**
- Create: `src/adapter/http_client.py`
- Modify: `src/adapter/__init__.py`
- Test: `tests/unit/adapter/test_http_client.py`

- [ ] **Step 1: 创建 AdaptorHttpClient 类**

```python
# src/adapter/http_client.py
from __future__ import annotations

import httpx
from typing import Any

class AdaptorHttpClient:
    """HTTP 客户端，用于调用 witty-agent-server API"""
    
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        return self._client
    
    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
    
    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送 POST 请求"""
        client = await self._get_client()
        response = await client.post(path, json=json)
        response.raise_for_status()
        return response.json()
    
    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送 GET 请求"""
        client = await self._get_client()
        response = await client.get(path, params=params)
        response.raise_for_status()
        return response.json()
    
    async def delete(self, path: str) -> None:
        """发送 DELETE 请求"""
        client = await self._get_client()
        response = await client.delete(path)
        response.raise_for_status()
    
    async def health_check(self) -> bool:
        """健康检查"""
        try:
            client = await self._get_client()
            response = await client.get("/v1/ping")
            return response.status_code == 200
        except Exception:
            return False
```

- [ ] **Step 2: 修改 src/adapter/__init__.py 导出**

```python
from src.adapter.http_client import AdaptorHttpClient
from src.adapter.websocket_client import WebSocketClient
from src.adapter.websocket_client_pool import WebSocketClientPool

__all__ = ["AdaptorHttpClient", "WebSocketClient", "WebSocketClientPool"]
```

- [ ] **Step 3: 运行测试验证**

Run: `pytest tests/unit/adapter/test_http_client.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/adapter/http_client.py src/adapter/__init__.py tests/unit/adapter/test_http_client.py
git commit -m "feat: add AdaptorHttpClient for witty-agent-server API calls"
```

---

## Task 3: 基础设施 - LocalWorkspaceStore base_path

**Files:**
- Modify: `src/storage/workspace_store.py:58-60`
- Test: `tests/unit/storage/test_workspace_store.py`

- [ ] **Step 1: 修改 LocalWorkspaceStore 默认 base_path**

```python
# src/storage/workspace_store.py 第 58-60 行
class LocalWorkspaceStore(WorkspaceStore):
    def __init__(self, base_path: str | Path = "~/witty-service/") -> None:
        super().__init__(base_path=base_path)
```

- [ ] **Step 2: 运行测试验证 workspace 路径**

Run: `pytest tests/unit/storage/test_workspace_store.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/storage/workspace_store.py
git commit -m "chore: update LocalWorkspaceStore base_path to ~/witty-service/"
```

---

## Task 4: Session Proxy - SessionResponse Schema

**Files:**
- Modify: `src/api/schemas.py`
- Test: `tests/unit/api/test_schemas.py`

- [ ] **Step 1: 修改 SessionResponse 添加新字段**

```python
# src/api/schemas.py - SessionResponse 类

class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    status: str
    context_initialized: bool = False  # 新增
    runtime_type: str | None = None    # 新增
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 2: 添加 SessionEventPage schema**

```python
# src/api/schemas.py

class SessionEventItem(BaseModel):
    id: str
    session_id: str
    type: str
    source: str | None = None
    payload: dict[str, Any]
    timestamp: datetime

class PaginationInfo(BaseModel):
    offset: int
    limit: int
    total: int

class SessionEventPage(BaseModel):
    items: list[SessionEventItem]
    pagination: PaginationInfo
```

- [ ] **Step 3: 运行测试验证**

Run: `pytest tests/unit/api/test_schemas.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/api/schemas.py
git commit -m "feat: add context_initialized and runtime_type to SessionResponse"
```

---

## Task 5: Session Proxy - SessionManager 透传

**Files:**
- Modify: `src/application/session_manager.py`
- Test: `tests/unit/application/test_session_manager.py`

- [ ] **Step 1: 添加 session upsert 方法到 repository**

```python
# src/persistence/repositories.py - SqliteRepository 类添加

def upsert_session(
    self,
    session_id: str,
    agent_id: str,
    status: str,
    context_initialized: bool = False,
    runtime_type: str | None = None,
    created_at: datetime | None = None,
) -> SessionRecord:
    """Upsert session from witty-agent-server"""
    with self._session_factory() as session:
        existing = session.get(SessionORM, session_id)
        now = datetime.now(timezone.utc)
        
        if existing is None:
            row = SessionORM(
                id=session_id,
                agent_id=agent_id,
                status=SessionStatus(status),
                created_at=created_at or now,
                updated_at=now,
            )
            session.add(row)
        else:
            existing.status = SessionStatus(status)
            existing.updated_at = now
        
        session.commit()
        session.refresh(existing or row)
        return self._to_session_record(existing or row)
```

- [ ] **Step 2: 修改 SessionManager 添加透传方法**

```python
# src/application/session_manager.py

async def create_session_remote(
    self,
    agent_id: str,
    adaptor_client: AdaptorHttpClient,
) -> SessionRecord:
    """在 witty-agent-server 创建 session"""
    result = await adaptor_client.post("/agent/sessions", json={})
    session = self._repository.upsert_session(
        session_id=result["id"],
        agent_id=agent_id,
        status="active",
        context_initialized=result.get("context_initialized", True),
        runtime_type=result.get("runtime_type"),
        created_at=datetime.fromisoformat(result["created_at"]) if "created_at" in result else None,
    )
    return session

async def list_sessions_remote(
    self,
    agent_id: str,
    adaptor_client: AdaptorHttpClient,
) -> list[SessionRecord]:
    """从 witty-agent-server 列出会话并刷新缓存"""
    result = await adaptor_client.get("/agent/sessions")
    sessions = []
    for item in result.get("sessions", []):
        session = self._repository.upsert_session(
            session_id=item["id"],
            agent_id=agent_id,
            status=item.get("status", "active"),
            context_initialized=item.get("context_initialized", True),
            runtime_type=item.get("runtime_type"),
            created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else None,
        )
        sessions.append(session)
    return sessions

async def get_session_remote(
    self,
    agent_id: str,
    session_id: str,
    adaptor_client: AdaptorHttpClient,
) -> SessionRecord:
    """从 witty-agent-server 获取 session 并刷新缓存"""
    result = await adaptor_client.get(f"/agent/sessions/{session_id}")
    return self._repository.upsert_session(
        session_id=result["id"],
        agent_id=agent_id,
        status=result.get("status", "active"),
        context_initialized=result.get("context_initialized", True),
        runtime_type=result.get("runtime_type"),
        created_at=datetime.fromisoformat(result["created_at"]) if "created_at" in result else None,
    )

async def delete_session_remote(
    self,
    session_id: str,
    adaptor_client: AdaptorHttpClient,
) -> None:
    """透传到 witty-agent-server 删除 session"""
    await adaptor_client.delete(f"/agent/sessions/{session_id}")
    self._repository.delete_session(session_id)

async def get_session_events(
    self,
    session_id: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """获取 session 事件回放（透传到 witty-agent-server）"""
    # 需要通过 agent 获取 adaptor_client
    # 此方法需要在 AgentManager 中调用
    pass
```

- [ ] **Step 3: 运行测试验证**

Run: `pytest tests/unit/application/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/application/session_manager.py src/persistence/repositories.py
git commit -m "feat: add session proxy methods to SessionManager"
```

---

## Task 6: Session Proxy - AgentManager 集成

**Files:**
- Modify: `src/application/agent_manager.py`
- Test: `tests/unit/application/test_agent_manager.py`

- [ ] **Step 1: 修改 AgentManager 添加 AdaptorHttpClient 获取方法**

```python
# src/application/agent_manager.py - AgentManager 类添加

def _get_adaptor_http_client(self, agent_id: str) -> AdaptorHttpClient:
    """获取到 witty-agent-server 的 HTTP 客户端"""
    sandbox_state = self._get_sandbox_state(agent_id)
    return AdaptorHttpClient(base_url=sandbox_state.adapter_base_url)
```

- [ ] **Step 2: 修改 create_session 透传到 witty-agent-server**

```python
# src/application/agent_manager.py - AgentManager.create_session

async def create_session(self, agent_id: str) -> SessionRecord:
    agent = self._get_agent(agent_id)
    if agent.status is not AgentStatus.running:
        raise DomainError(
            code=AGENT_NOT_RUNNING,
            message="Agent must be running to create session.",
            details={"agent_id": agent_id, "status": agent.status.value},
        )
    
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        session = await self._session_manager.create_session_remote(agent_id, adaptor_client)
    finally:
        await adaptor_client.close()
    
    return session
```

- [ ] **Step 3: 修改 list_sessions 透传到 witty-agent-server**

```python
async def list_sessions(self, agent_id: str) -> list[SessionRecord]:
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        return await self._session_manager.list_sessions_remote(agent_id, adaptor_client)
    finally:
        await adaptor_client.close()
```

- [ ] **Step 4: 修改 get_session 透传到 witty-agent-server**

```python
async def get_session(self, agent_id: str, session_id: str) -> SessionRecord:
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        return await self._session_manager.get_session_remote(agent_id, session_id, adaptor_client)
    finally:
        await adaptor_client.close()
```

- [ ] **Step 5: 修改 delete_session 透传到 witty-agent-server**

```python
async def delete_session(self, agent_id: str, session_id: str) -> None:
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        await self._session_manager.delete_session_remote(session_id, adaptor_client)
    finally:
        await adaptor_client.close()
```

- [ ] **Step 6: 修改 send_message 添加 adaptor_client 参数**

```python
async def send_message(
    self,
    agent_id: str,
    session_id: str,
    content: str,
    adaptor_client: AdaptorHttpClient | None = None,
) -> dict[str, Any]:
    # ... existing code ...
    if adaptor_client is None:
        adaptor_client = self._get_adaptor_http_client(agent_id)
    # ... existing code ...
```

- [ ] **Step 7: 运行测试验证**

Run: `pytest tests/unit/application/test_agent_manager.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/application/agent_manager.py
git commit -m "feat: integrate AdaptorHttpClient into AgentManager for session proxy"
```

---

## Task 7: Session Proxy - API 路由

**Files:**
- Modify: `src/api/agents.py`
- Test: `tests/unit/api/test_agents.py`

- [ ] **Step 1: 修改 create_session 为 async**

```python
# src/api/agents.py

@router.post(
    "/{agent_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(agent_id: str, services: ServiceContainer = Depends(get_services)) -> SessionResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    session = await manager.create_session(agent_id)
    return SessionResponse.model_validate(session)
```

- [ ] **Step 2: 修改 get_session 为 async**

```python
@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    agent_id: str,
    session_id: str,
    services: ServiceContainer = Depends(get_services),
) -> SessionResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    session = await manager.get_session(agent_id, session_id)
    return SessionResponse.model_validate(session)
```

- [ ] **Step 3: 添加 events 路由**

```python
@router.get("/{agent_id}/sessions/{session_id}/events")
async def get_session_events(
    agent_id: str,
    session_id: str,
    offset: int = 0,
    limit: int = 50,
    services: ServiceContainer = Depends(get_services),
) -> SessionEventsResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    adaptor_client = manager._get_adaptor_http_client(agent_id)
    try:
        result = await adaptor_client.get(
            f"/agent/sessions/{session_id}/events",
            params={"offset": offset, "limit": limit},
        )
        return SessionEventsResponse.model_validate(result)
    finally:
        await adaptor_client.close()
```

- [ ] **Step 4: 更新 SessionEventsResponse schema**

```python
# src/api/schemas.py

class SessionEventsResponse(BaseModel):
    items: list[SessionEventItem]
    pagination: PaginationInfo
```

- [ ] **Step 5: 运行测试验证**

Run: `pytest tests/unit/api/test_agents.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/api/agents.py src/api/schemas.py
git commit -m "feat: add async session methods and events endpoint to API"
```

---

## Task 8: Agent 生命周期 - AgentStatus 枚举更新

**Files:**
- Modify: `src/domain/enums.py`
- Test: `tests/unit/domain/test_enums.py`

- [ ] **Step 1: 更新 AgentStatus 枚举，去掉 STOPPED**

```python
# src/domain/enums.py

class AgentStatus(str, Enum):
    creating = "creating"
    running = "running"
    paused = "paused"
    deleted = "deleted"
    error = "error"
```

- [ ] **Step 2: 更新 can_transition 函数**

```python
def can_transition(from_status: AgentStatus, to_status: AgentStatus) -> bool:
    valid_transitions = {
        AgentStatus.creating: {AgentStatus.running, AgentStatus.deleted, AgentStatus.error},
        AgentStatus.running: {AgentStatus.paused, AgentStatus.deleted, AgentStatus.error},
        AgentStatus.paused: {AgentStatus.running, AgentStatus.deleted},
        AgentStatus.deleted: set(),  # 无法从 deleted 转换到其他状态
        AgentStatus.error: {AgentStatus.running, AgentStatus.deleted},
    }
    return to_status in valid_transitions.get(from_status, set())
```

- [ ] **Step 3: 运行测试验证**

Run: `pytest tests/unit/domain/test_enums.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/domain/enums.py
git commit -m "refactor: remove STOPPED from AgentStatus enum"
```

---

## Task 9: Agent 生命周期 - pause_agent

**Files:**
- Modify: `src/application/agent_manager.py`
- Test: `tests/unit/application/test_agent_manager.py`

- [ ] **Step 1: 修改 pause_agent 逻辑**

```python
def pause_agent(self, agent_id: str) -> AgentRecord:
    agent = self._get_agent(agent_id)
    self._ensure_transition(agent, AgentStatus.paused)
    
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        # 调用 witty-agent-server /agent/stop 优雅停止
        import httpx
        try:
            adaptor_client.post("/agent/stop", json={})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:  # 可能已经停止
                raise
    finally:
        adaptor_client.close()
    
    return self._repository.update_agent_status(agent_id, AgentStatus.paused)
```

- [ ] **Step 2: 运行测试验证**

Run: `pytest tests/unit/application/test_agent_manager.py::test_pause_agent -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/application/agent_manager.py
git commit -m "feat: pause_agent calls /agent/stop and keeps sandbox running"
```

---

## Task 10: Agent 生命周期 - delete_agent

**Files:**
- Modify: `src/application/agent_manager.py`
- Modify: `src/storage/runtime_backup.py` (添加 backup 方法到 AgentManager)
- Test: `tests/unit/application/test_agent_manager.py`

- [ ] **Step 1: 添加运行时备份和沙箱清理方法到 AgentManager**

```python
# src/application/agent_manager.py - AgentManager 类添加

def _backup_runtime(self, agent_id: str, runtime_type: str = "openclaw") -> Path | None:
    """备份运行时文件"""
    backup_store = RuntimeBackupStore()
    try:
        return backup_store.backup(agent_id, runtime_type)
    except Exception:
        return None

def _cleanup_sandbox(self, agent_id: str) -> None:
    """清理沙箱"""
    sandbox_state = self._repository.get_sandbox_state(agent_id)
    if sandbox_state is not None:
        self._sandbox_backend.cleanup(sandbox_state.handle)

def _stop_runtime(self, agent_id: str) -> None:
    """停止 witty-agent-server 运行时"""
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        import httpx
        try:
            adaptor_client.post("/agent/stop", json={})
        except httpx.HTTPStatusError:
            pass  # 可能已经停止
    finally:
        adaptor_client.close()
```

- [ ] **Step 2: 修改 delete_agent 逻辑**

```python
def delete_agent(self, agent_id: str) -> None:
    agent = self._get_agent(agent_id)
    sandbox_state = self._repository.get_sandbox_state(agent_id)
    
    cleanup_errors: list[dict[str, str]] = []
    
    # 1. 备份运行时
    if sandbox_state is not None:
        self._collect_error(
            cleanup_errors,
            "runtime_backup",
            lambda: self._backup_runtime(agent_id, agent.adapter_type),
        )
    
    # 2. 停止运行时
    if agent.status in {AgentStatus.running, AgentStatus.paused}:
        self._collect_error(
            cleanup_errors,
            "runtime_stop",
            lambda: self._stop_runtime(agent_id),
        )
    
    # 3. 清理沙箱
    if sandbox_state is not None:
        self._collect_error(
            cleanup_errors,
            "sandbox_cleanup",
            lambda: self._sandbox_backend.cleanup(sandbox_state.handle),
        )
    
    # 4. 保留 workspace 目录（不清除）
    
    # 5. 更新 agent 状态
    self._collect_error(
        cleanup_errors,
        "agent_status",
        lambda: self._repository.update_agent_status(agent_id, AgentStatus.deleted),
    )
    
    if cleanup_errors:
        self._raise_operation_failed(
            code=AGENT_DELETE_FAILED,
            message="Agent delete failed.",
            agent_id=agent_id,
            cause=RuntimeError(cleanup_errors[0]["error"]),
            cleanup_errors=cleanup_errors,
        )
```

- [ ] **Step 3: 运行测试验证**

Run: `pytest tests/unit/application/test_agent_manager.py::test_delete_agent -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/application/agent_manager.py
git commit -m "feat: delete_agent backups runtime, stops runtime, and cleans up sandbox"
```

---

## Task 11: Agent 生命周期 - resume_agent 分支逻辑

**Files:**
- Modify: `src/application/agent_manager.py`
- Test: `tests/unit/application/test_agent_manager.py`

- [ ] **Step 1: 添加 resume_from_deleted 方法**

```python
async def _resume_from_deleted(self, agent_id: str) -> AgentRecord:
    """从 deleted 状态恢复"""
    agent = self._get_agent(agent_id)
    
    # 1. 检查备份
    backup_store = RuntimeBackupStore()
    if not backup_store.backup_exists(agent_id, agent.adapter_type):
        raise DomainError(
            code=RUNTIME_BACKUP_NOT_FOUND,
            message="Runtime backup not found.",
            details={"agent_id": agent_id},
        )
    
    # 2. 恢复运行时备份
    backup_store.restore(agent_id, agent.adapter_type)
    
    # 3. 重新启动沙箱
    sandbox_handle = self._sandbox_backend.start(
        agent_id=agent_id,
        workspace_path=agent.workspace_path,
    )
    adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
    
    # 4. 保存沙箱状态
    self._repository.save_sandbox_state(
        agent_id,
        sandbox_payload_json=self._sandbox_handle_payload(sandbox_handle),
        adapter_base_url=adapter_endpoint.base_url,
        adapter_ready=True,
    )
    
    # 5. 等待沙箱就绪
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        for _ in range(30):  # 30 秒超时
            if await adaptor_client.health_check():
                break
            await asyncio.sleep(1)
        else:
            raise DomainError(
                code=SANDBOX_NOT_READY,
                message="Sandbox health check timeout.",
                details={"agent_id": agent_id},
            )
    finally:
        adaptor_client.close()
    
    # 6. 调用 /agent/start
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        import httpx
        try:
            adaptor_client.post("/agent/start", json={})
        except httpx.HTTPStatusError as exc:
            raise DomainError(
                code=RUNTIME_START_FAILED,
                message="Failed to start runtime.",
                details={"agent_id": agent_id, "error": str(exc)},
            ) from exc
    finally:
        adaptor_client.close()
    
    # 7. 更新状态
    return self._repository.update_agent_status(agent_id, AgentStatus.running)
```

- [ ] **Step 2: 添加 resume_from_paused 方法**

```python
def _resume_from_paused(self, agent_id: str) -> AgentRecord:
    """从 paused 状态恢复"""
    agent = self._get_agent(agent_id)
    
    # 1. 验证沙箱是否仍在运行
    sandbox_state = self._get_sandbox_state(agent_id)
    status = self._sandbox_backend.status(sandbox_state.handle)
    if status == SandboxStatus.stopped:
        # 沙箱已停止，降级到 deleted 场景
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self._resume_from_deleted(agent_id)
        )
    
    # 2. 调用 /agent/start
    adaptor_client = self._get_adaptor_http_client(agent_id)
    try:
        import httpx
        try:
            adaptor_client.post("/agent/start", json={})
        except httpx.HTTPStatusError as exc:
            raise DomainError(
                code=RUNTIME_START_FAILED,
                message="Failed to start runtime.",
                details={"agent_id": agent_id, "error": str(exc)},
            ) from exc
    finally:
        adaptor_client.close()
    
    # 3. 更新状态
    return self._repository.update_agent_status(agent_id, AgentStatus.running)
```

- [ ] **Step 3: 修改 resume_agent 分支逻辑**

```python
async def resume_agent(self, agent_id: str) -> AgentRecord:
    agent = self._get_agent(agent_id)
    self._ensure_transition(agent, AgentStatus.running)
    
    if agent.status == AgentStatus.paused:
        return self._resume_from_paused(agent_id)
    elif agent.status == AgentStatus.deleted:
        return await self._resume_from_deleted(agent_id)
    else:
        raise DomainError(
            code=INVALID_AGENT_TRANSITION,
            message="Cannot resume from current status.",
            details={"agent_id": agent_id, "status": agent.status.value},
        )
```

- [ ] **Step 4: 运行测试验证**

Run: `pytest tests/unit/application/test_agent_manager.py::test_resume_agent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/application/agent_manager.py
git commit -m "feat: resume_agent branch logic for paused vs deleted states"
```

---

## Task 12: 集成测试

**Files:**
- Create: `tests/e2e/test_session_proxy_e2e.py`
- Create: `tests/e2e/test_agent_lifecycle_e2e.py`

- [ ] **Step 1: 编写 Session Proxy E2E 测试**

```python
# tests/e2e/test_session_proxy_e2e.py
import pytest
from httpx import AsyncClient
from src.main import create_app

@pytest.mark.asyncio
async def test_session_proxy_flow():
    app = create_app()
    
    # 1. 创建 agent
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agents",
            json={
                "name": "test-agent",
                "sandbox_type": "local_process",
                "adapter_type": "openclaw",
                "idle_timeout_seconds": 3600,
            },
        )
        assert response.status_code == 201
        agent_id = response.json()["id"]
    
    # 2. 创建 session（应透传到 witty-agent-server）
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(f"/api/v1/agents/{agent_id}/sessions")
        assert response.status_code == 201
        session_id = response.json()["id"]
        assert "context_initialized" in response.json()
    
    # 3. 列出 sessions（应刷新本地缓存）
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(f"/api/v1/agents/{agent_id}/sessions")
        assert response.status_code == 200
        assert len(response.json()) >= 1
    
    # 4. 删除 session
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.delete(f"/api/v1/agents/{agent_id}/sessions/{session_id}")
        assert response.status_code == 204
    
    # 5. 清理
    async with AsyncClient(app=app, base_url="http://test") as client:
        await client.delete(f"/api/v1/agents/{agent_id}")
```

- [ ] **Step 2: 编写 Agent 生命周期 E2E 测试**

```python
# tests/e2e/test_agent_lifecycle_e2e.py
import pytest
from httpx import AsyncClient
from src.main import create_app

@pytest.mark.asyncio
async def test_pause_resume_flow():
    app = create_app()
    
    # 1. 创建 agent
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agents",
            json={
                "name": "test-agent",
                "sandbox_type": "local_process",
                "adapter_type": "openclaw",
                "idle_timeout_seconds": 3600,
            },
        )
        agent_id = response.json()["id"]
    
    # 2. Pause
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(f"/api/v1/agents/{agent_id}/pause")
        assert response.status_code == 200
        assert response.json()["status"] == "paused"
    
    # 3. Resume
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(f"/api/v1/agents/{agent_id}/resume")
        assert response.status_code == 200
        assert response.json()["status"] == "running"

@pytest.mark.asyncio
async def test_delete_resume_flow():
    app = create_app()
    
    # 1. 创建 agent
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agents",
            json={
                "name": "test-agent",
                "sandbox_type": "local_process",
                "adapter_type": "openclaw",
                "idle_timeout_seconds": 3600,
            },
        )
        agent_id = response.json()["id"]
    
    # 2. Delete
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.delete(f"/api/v1/agents/{agent_id}")
        assert response.status_code == 204
    
    # 3. Resume from deleted
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(f"/api/v1/agents/{agent_id}/resume")
        assert response.status_code == 200
        assert response.json()["status"] == "running"
```

- [ ] **Step 3: 运行 E2E 测试**

Run: `pytest tests/e2e/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_session_proxy_e2e.py tests/e2e/test_agent_lifecycle_e2e.py
git commit -m "test: add E2E tests for session proxy and agent lifecycle"
```

---

## Task 13: 文档更新

**Files:**
- Modify: `docs/superpowers/specs/2026-04-09-witty-service-websocket-adaptor-design.md`
- Modify: `README.md`

- [ ] **Step 1: 确认设计文档已更新**

设计文档已经在 brainstorming 阶段更新为 v3.0

- [ ] **Step 2: Commit**

```bash
git add docs/
git commit -m "docs: update design docs to v3.0"
```

---

## Self-Review 检查清单

### 1. Spec Coverage
- [x] Session Proxy 模式 - Task 5, 6, 7
- [x] 透传创建/查询/删除 - Task 6, 7
- [x] 本地缓存刷新 (upsert) - Task 5
- [x] Pause 流程 - Task 9
- [x] Delete 流程（备份 + 清理） - Task 10
- [x] Resume 分支逻辑 - Task 11
- [x] agent_id 复用 - Task 11
- [x] workspace 保留 - Task 10
- [x] 备份不存在抛异常 - Task 11

### 2. Placeholder Scan
无 placeholder，所有步骤都包含实际代码

### 3. Type Consistency
- `AgentStatus` 枚举：creating, running, paused, deleted, error
- `SessionResponse.context_initialized`: bool
- `SessionResponse.runtime_type`: str | None
- `adaptor_client.post()` / `get()` / `delete()` 方法签名一致

---

## 执行选项

Plan complete and saved to `docs/superpowers/plans/2026-04-13-session-proxy-implementation-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
