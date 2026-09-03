from __future__ import annotations

from typing import Any

import pytest

from witty_agent_server.runtimes.artifact_detector import (
    INLINE_CONTENT_LIMIT,
    artifact_completed_event,
    artifact_started_event,
    detect_artifact,
    is_sensitive_file,
    normalize_relative_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("output/demo.html", "output/demo.html"),
        ("./output/demo.html", "output/demo.html"),
    ],
)
def test_normalize_relative_path_accepts_valid(raw: str, expected: str) -> None:
    assert normalize_relative_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "output/../secret.html",
        "../output/demo.html",
        None,
        123,
    ],
)
def test_normalize_relative_path_rejects_invalid(raw: Any) -> None:
    assert normalize_relative_path(raw) is None


def test_normalize_relative_path_keeps_absolute_workspace_path() -> None:
    raw = (
        "/root/.witty/agent-workspaces/"
        "b07433d1-0416-4a48-b24b-a1123f7abe0a/workspace/output/demo.html"
    )
    # 绝对路径临时放行：原样保留，交由 witty-service 边界做 relative_to 归一化。
    assert normalize_relative_path(raw) == raw


def test_normalize_relative_path_keeps_plain_absolute_path() -> None:
    assert normalize_relative_path("/abs/demo.html") == "/abs/demo.html"


@pytest.mark.parametrize(
    ("path_value", "expected_type", "expected_mime"),
    [
        ("output/demo.html", "html", "text/html"),
        ("output/logo.png", "image", "image/png"),
        ("output/video.mp4", "video", "video/mp4"),
        ("notes.md", "markdown", "text/markdown"),
        ("src/notes.md", "markdown", "text/markdown"),
        ("app.js", "code", "text/javascript"),
        ("src/app.py", "code", "text/x-python"),
        ("src/main.ts", "code", "text/typescript"),
        ("scripts/run.sh", "code", "text/x-shellscript"),
        ("config/deploy.yaml", "code", "application/yaml"),
        ("docs/report.pdf", "pdf", "application/pdf"),
    ],
)
def test_detect_artifact_hits_whitelist(
    path_value: str, expected_type: str, expected_mime: str
) -> None:
    meta = detect_artifact(path_value)
    assert meta is not None
    assert meta["id"] == path_value
    assert meta["name"] == path_value.rsplit("/", 1)[-1]
    assert meta["type"] == expected_type
    assert meta["mime"] == expected_mime
    assert meta["version"] == 1


@pytest.mark.parametrize(
    "path_value",
    [
        "src/main.txt",  # 扩展名不在白名单
        "output/notes.txt",  # 扩展名不在白名单
        "output/sub/../../etc/passwd",
        "demo.exe",
    ],
)
def test_detect_artifact_misses_whitelist(path_value: str) -> None:
    assert detect_artifact(path_value) is None


def _write_args(**overrides: str) -> dict[str, str]:
    return {
        "file_path": "output/demo.html",
        "content": "<h1>hi</h1>",
        **overrides,
    }


def test_artifact_started_carries_metadata_without_content() -> None:
    payload = artifact_started_event(_write_args())
    assert payload is not None
    assert payload["id"] == "output/demo.html"
    assert payload["status"] == "creating"
    assert payload["type"] == "html"
    assert payload["size"] == len("<h1>hi</h1>")
    assert payload["mime"] == "text/html"
    assert "content" not in payload


def test_artifact_events_accept_root_level_paths() -> None:
    args = _write_args(file_path="留白.md")
    started = artifact_started_event(args)
    assert started is not None
    assert started["relative_path"] == "留白.md"
    assert started["type"] == "markdown"
    completed = artifact_completed_event(args)
    assert completed is not None
    assert completed["status"] == "ready"
    assert completed["content"] == "<h1>hi</h1>"


def test_artifact_completed_inlines_text_within_limit() -> None:
    payload = artifact_completed_event(_write_args())
    assert payload is not None
    assert payload["status"] == "ready"
    assert payload["content"] == "<h1>hi</h1>"
    assert payload["size"] == len("<h1>hi</h1>")


def test_artifact_completed_omits_oversized_or_binary_content() -> None:
    oversized = artifact_completed_event(
        _write_args(content="x" * (INLINE_CONTENT_LIMIT + 1))
    )
    assert oversized is not None
    assert oversized["size"] == INLINE_CONTENT_LIMIT + 1
    assert "content" not in oversized

    binary = artifact_completed_event(_write_args(file_path="output/logo.png"))
    assert binary is not None
    assert binary["type"] == "image"
    assert binary["mime"] == "image/png"
    assert "content" not in binary


def test_artifact_completed_error_status_omits_content() -> None:
    payload = artifact_completed_event(_write_args(), is_error=True)
    assert payload is not None
    assert payload["status"] == "error"
    assert "content" not in payload


def test_artifact_event_none_outside_whitelist_and_accepts_json_args() -> None:
    assert artifact_started_event({"file_path": "src/main.txt", "content": "x"}) is None
    assert (
        artifact_completed_event(_write_args(file_path="output/../demo.html")) is None
    )

    # 绝对路径临时放行：事件原样携带绝对路径，由 witty-service 边界归一化。
    absolute = artifact_completed_event(
        _write_args(
            file_path=(
                "/root/.witty/agent-workspaces/"
                "b07433d1-0416-4a48-b24b-a1123f7abe0a/workspace/output/demo.html"
            )
        )
    )
    assert absolute is not None
    assert (
        absolute["relative_path"]
        == "/root/.witty/agent-workspaces/"
        "b07433d1-0416-4a48-b24b-a1123f7abe0a/workspace/output/demo.html"
    )
    assert absolute["content"] == "<h1>hi</h1>"

    payload = artifact_completed_event(
        '{"file_path": "output/demo.html", "content": "<p>ok</p>"}'
    )
    assert payload is not None
    assert payload["content"] == "<p>ok</p>"


@pytest.mark.parametrize(
    "file_path",
    [
        "config/credentials.json",
        "deploy/secrets.yaml",
        "secrets.py",
        "data/credentials.csv",
    ],
)
def test_artifact_completed_omits_content_for_sensitive_files(
    file_path: str,
) -> None:
    payload = artifact_completed_event(_write_args(file_path=file_path))
    assert payload is not None
    assert payload["status"] == "ready"
    assert payload["relative_path"] == file_path
    assert "content" not in payload


@pytest.mark.parametrize(
    "file_path",
    [
        "output/demo.html",
        "src/app.py",
        "config/deploy.yaml",
        "tokenizer.py",  # 名称含 token 但不属于敏感文件，刻意不命中
    ],
)
def test_artifact_completed_keeps_content_for_regular_files(
    file_path: str,
) -> None:
    payload = artifact_completed_event(_write_args(file_path=file_path))
    assert payload is not None
    assert payload["content"] == "<h1>hi</h1>"


@pytest.mark.parametrize(
    "file_name",
    [
        ".env",
        ".env.local",
        "credentials.json",
        "secrets.yaml",
        "kubeconfig",
        "server.key",
        "id_ed25519",
    ],
)
def test_is_sensitive_file_matches(file_name: str) -> None:
    assert is_sensitive_file(file_name) is True


@pytest.mark.parametrize(
    "file_name",
    [
        "demo.html",
        "app.py",
        "README.md",
        "tokenizer.py",
        "deploy.yaml",
    ],
)
def test_is_sensitive_file_ignores_regular_names(file_name: str) -> None:
    assert is_sensitive_file(file_name) is False
