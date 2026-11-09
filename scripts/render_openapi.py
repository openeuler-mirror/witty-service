"""重新生成 docs/agent-session-workspace-openapi.yaml。"""

from __future__ import annotations

from pathlib import Path

from witty_service.api.contract.openapi import render_contract_openapi_yaml

TARGET = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "agent-session-workspace-openapi.yaml"
)


def main() -> None:
    TARGET.write_text(render_contract_openapi_yaml(), encoding="utf-8")


if __name__ == "__main__":
    main()
