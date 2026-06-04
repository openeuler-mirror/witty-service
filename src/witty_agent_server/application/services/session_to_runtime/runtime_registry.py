from witty_agent_server.runtimes.runtime_base import RuntimeBase


class RuntimeRegistry:
    """Session 服务使用的运行时注册表实现。"""

    def __init__(self) -> None:
        self._runtimes: dict[str, RuntimeBase] = {}

    def register(self, runtime: RuntimeBase) -> None:
        """按 runtime_type 注册运行时实例。"""
        self._runtimes[runtime.runtime_type] = runtime

    def get(self, runtime_type: str) -> RuntimeBase | None:
        """按 runtime_type 获取运行时实例。"""
        return self._runtimes.get(runtime_type)
