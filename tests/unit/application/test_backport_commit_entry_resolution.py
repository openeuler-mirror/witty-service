from __future__ import annotations

from unittest.mock import patch

import pytest

from witty_service.application.backport_git_client import (
    BackportGitClient,
    CommitTitleResolution,
)
from witty_service.application.backport_service import BackportService
from witty_service.domain.errors import DomainError


def test_resolve_commits_by_title_prefers_case_insensitive_exact() -> None:
    result = type(
        "Result",
        (),
        {
            "returncode": 0,
            "stdout": (
                "exact-sha\x1fFix: Correct Import\n"
                "contains-sha\x1fFix: Correct Import (follow-up)\n"
            ),
            "stderr": "",
        },
    )()
    with (
        patch.object(BackportGitClient, "ensure_git_repo"),
        patch.object(BackportGitClient, "_run_git", return_value=result) as run_git,
    ):
        resolutions = (
            BackportGitClient.resolve_commits_by_title(
                "/source", "main", ["fix: correct import"]
            )
        )

    assert resolutions == {"fix: correct import": CommitTitleResolution("exact-sha")}
    assert run_git.call_args.args[1] == [
        "log",
        "main",
        "--no-merges",
        "--format=%H%x1f%s",
    ]


def test_resolve_commits_by_title_reports_ambiguous_contains_matches() -> None:
    result = type(
        "Result",
        (),
        {
            "returncode": 0,
            "stdout": "one\x1fFix import one\ntwo\x1fFix import two\n",
            "stderr": "",
        },
    )()
    with (
        patch.object(BackportGitClient, "ensure_git_repo"),
        patch.object(BackportGitClient, "_run_git", return_value=result),
    ):
        resolution = BackportGitClient.resolve_commits_by_title(
            "/source", "main", ["fix import"]
        )["fix import"]

    assert resolution.commit is None
    assert resolution.error and "多个候选" in resolution.error


def test_resolve_commits_by_title_keeps_case_sensitive_exact_inputs_separate() -> None:
    result = type(
        "Result",
        (),
        {
            "returncode": 0,
            "stdout": "upper\x1fFix import\nlower\x1ffix import\n",
            "stderr": "",
        },
    )()
    with (
        patch.object(BackportGitClient, "ensure_git_repo"),
        patch.object(BackportGitClient, "_run_git", return_value=result),
    ):
        resolutions = BackportGitClient.resolve_commits_by_title(
            "/source", "main", ["Fix import", "fix import"]
        )

    assert resolutions == {
        "Fix import": CommitTitleResolution("upper"),
        "fix import": CommitTitleResolution("lower"),
    }


def test_resolve_commit_entries_prefers_title_then_archives_resolved_sha() -> None:
    service = BackportService.__new__(BackportService)
    payload = {"commit_entries": [{"commit": "abcdef1", "commit_title": "Fix import"}]}
    config = {"project_dir": "/source", "source_branch": "main"}
    with (
        patch.object(
            BackportGitClient,
            "resolve_commits_by_title",
            return_value={"Fix import": CommitTitleResolution("actual-sha")},
        ) as resolve_title,
        patch.object(BackportGitClient, "resolve_commit") as resolve_sha,
    ):
        assert service._validated_payload_commit_entries(payload, config) == [
            {"commit": "actual-sha", "commit_title": "Fix import"}
        ]
    resolve_title.assert_called_once()
    resolve_sha.assert_not_called()


def test_resolve_commit_entries_falls_back_to_sha_with_row_diagnostics() -> None:
    service = BackportService.__new__(BackportService)
    payload = {"commit_entries": [{"commit": "abcdef1", "commit_title": "Fix import"}]}
    config = {"project_dir": "/source", "source_branch": "main"}
    with (
        patch.object(
            BackportGitClient,
            "resolve_commits_by_title",
            return_value={},
        ),
        patch.object(
            BackportGitClient, "resolve_commit", side_effect=RuntimeError("no sha")
        ),
        pytest.raises(DomainError) as exc_info,
    ):
        service._validated_payload_commit_entries(payload, config)

    assert exc_info.value.details["errors"] == [
        {
            "row": 1,
            "field": "commit",
            "message": "标题解析失败：无法根据 commit_title 找到提交：Fix import；commit_id 解析失败：no sha",
        }
    ]
