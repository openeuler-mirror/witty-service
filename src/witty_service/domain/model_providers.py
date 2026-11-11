from __future__ import annotations

DEFAULT_API_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1",
    "ollama": "http://localhost:11434/v1",
    "azure": "https://{resource}.openai.azure.com",
    "deepseek": "https://api.deepseek.com/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimax.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "custom": "",
}

PROVIDERS_REQUIRING_KEY: frozenset[str] = frozenset(DEFAULT_API_BASE_URLS) - {
    "ollama",
    "custom",
}
