from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_COMMIT_IMPORT_BYTES = 1024 * 1024
MAX_COMMIT_IMPORT_ENTRIES = 5000
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,}$")
COMMIT_ID_HEADER_ALIASES = {"commithash", "commit", "hash", "sha", "commitid"}
COMMIT_TITLE_HEADER_ALIASES = {"committitle", "title", "subject", "patchtitle"}


@dataclass(frozen=True, slots=True)
class CommitImportResult:
    entries: list[dict[str, str]]
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"entries": self.entries}
        if self.errors:
            result["errors"] = self.errors
        if self.warnings:
            result["warnings"] = self.warnings
        if self.rows:
            result["rows"] = self.rows
        return result


def _normalize_header(value: str) -> str:
    return "".join(
        character for character in value.strip().lower() if character.isalnum()
    )


def _resolve_header_columns(row: list[str]) -> tuple[int, int] | None:
    """Return commit/title indexes for a recognized two-column legacy header."""
    if len(row) != 2:
        return None
    normalized = [_normalize_header(value) for value in row]
    commit_indexes = [
        index
        for index, value in enumerate(normalized)
        if value in COMMIT_ID_HEADER_ALIASES
    ]
    title_indexes = [
        index
        for index, value in enumerate(normalized)
        if value in COMMIT_TITLE_HEADER_ALIASES
    ]
    if len(commit_indexes) != 1 or len(title_indexes) != 1:
        return None
    return commit_indexes[0], title_indexes[0]


def parse_commit_import(
    content: bytes, *, delimiter: Literal["csv", "tsv"]
) -> CommitImportResult:
    """Parse browser-provided CSV/TSV into the canonical Backport entry shape."""
    if len(content) > MAX_COMMIT_IMPORT_BYTES:
        return CommitImportResult(
            entries=[],
            errors=[
                {
                    "field": "file",
                    "message": f"导入内容不能超过 {MAX_COMMIT_IMPORT_BYTES // 1024 // 1024} MiB。",
                }
            ],
            warnings=[],
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return CommitImportResult(
            entries=[],
            errors=[{"field": "file", "message": "导入内容必须使用 UTF-8 编码。"}],
            warnings=[],
        )

    separator = "," if delimiter == "csv" else "\t"
    try:
        rows = list(
            csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True)
        )
    except csv.Error as error:
        return CommitImportResult(
            entries=[],
            errors=[
                {"field": "file", "message": f"{delimiter.upper()} 格式错误：{error}"}
            ],
            warnings=[],
        )

    numbered_rows = [
        (index, row)
        for index, row in enumerate(rows, start=1)
        if any(cell.strip() for cell in row)
    ]
    commit_index, title_index = 0, 1
    if numbered_rows:
        header_columns = _resolve_header_columns(numbered_rows[0][1])
        if header_columns is not None:
            commit_index, title_index = header_columns
            numbered_rows = numbered_rows[1:]

    raw_entries: list[tuple[int, dict[str, str]]] = []
    preview_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row_number, row in numbered_rows:
        preview_rows.append(
            {
                "row": row_number,
                "commit": row[commit_index] if len(row) > commit_index else "",
                "commit_title": row[title_index] if len(row) > title_index else "",
            }
        )
        if len(row) != 2:
            errors.append(
                {
                    "row": row_number,
                    "field": "row",
                    "message": "每行必须恰好包含 commit_id 和 commit_title 两列。",
                }
            )
            continue
        raw_entries.append(
            (
                row_number,
                {"commit": row[commit_index], "commit_title": row[title_index]},
            )
        )
    validated = validate_commit_entries(raw_entries)
    return CommitImportResult(
        entries=validated.entries,
        errors=[*errors, *validated.errors],
        warnings=validated.warnings,
        rows=preview_rows,
    )


def validate_commit_entries(
    entries: Iterable[Mapping[str, Any] | tuple[int, Mapping[str, Any]]],
) -> CommitImportResult:
    """Validate manually edited entries as authoritatively as imported text.

    ``row`` is optional for the caller but is always included for entry-level errors.
    Identical duplicates keep their first occurrence; conflicting titles are errors.
    """
    materialized = list(entries)
    if len(materialized) > MAX_COMMIT_IMPORT_ENTRIES:
        return CommitImportResult(
            entries=[],
            errors=[
                {
                    "field": "entries",
                    "message": f"提交条目不能超过 {MAX_COMMIT_IMPORT_ENTRIES} 条。",
                }
            ],
            warnings=[],
        )

    normalized: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    first_for_sha: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(materialized, start=1):
        row = index
        entry: Mapping[str, Any]
        if isinstance(item, tuple):
            if (
                len(item) != 2
                or not isinstance(item[0], int)
                or not isinstance(item[1], Mapping)
            ):
                errors.append(
                    {
                        "row": index,
                        "field": "entry",
                        "message": "提交条目必须是对象，且包含 commit 与 commit_title。",
                    }
                )
                continue
            row, entry = item
        elif isinstance(item, Mapping):
            entry = item
        else:
            errors.append(
                {
                    "row": row,
                    "field": "entry",
                    "message": "提交条目必须是对象，且包含 commit 与 commit_title。",
                }
            )
            continue
        commit = entry.get("commit")
        title = entry.get("commit_title")
        commit_text = commit.strip() if isinstance(commit, str) else ""
        title_text = title.strip() if isinstance(title, str) else ""
        if not SHA_PATTERN.fullmatch(commit_text):
            errors.append(
                {
                    "row": row,
                    "field": "commit",
                    "message": "commit_id 必须是至少 7 位的十六进制 Git SHA。",
                }
            )
        if not title_text:
            errors.append(
                {
                    "row": row,
                    "field": "commit_title",
                    "message": "commit_title 不能为空。",
                }
            )
        elif "\n" in title_text or "\r" in title_text:
            errors.append(
                {
                    "row": row,
                    "field": "commit_title",
                    "message": "commit_title 必须是单行文本。",
                }
            )
        if (
            not SHA_PATTERN.fullmatch(commit_text)
            or not title_text
            or "\n" in title_text
            or "\r" in title_text
        ):
            continue
        commit_text = commit_text.lower()
        previous = first_for_sha.get(commit_text)
        if previous is not None:
            previous_row, previous_title = previous
            if previous_title == title_text:
                warnings.append(
                    {
                        "row": row,
                        "message": f"与第 {previous_row} 行重复，已保留首次出现的提交。",
                    }
                )
            else:
                errors.append(
                    {
                        "row": row,
                        "field": "commit_title",
                        "message": f"与第 {previous_row} 行使用相同 commit_id 但 commit_title 不同。",
                    }
                )
            continue
        first_for_sha[commit_text] = (row, title_text)
        normalized.append({"commit": commit_text, "commit_title": title_text})

    if not normalized and not errors:
        errors.append({"field": "entries", "message": "至少需要一条提交。"})
    if (
        not errors
        and len(serialize_commit_entries(normalized).encode("utf-8"))
        > MAX_COMMIT_IMPORT_BYTES
    ):
        return CommitImportResult(
            entries=[],
            errors=[
                {
                    "field": "entries",
                    "message": f"提交清单不能超过 {MAX_COMMIT_IMPORT_BYTES // 1024 // 1024} MiB。",
                }
            ],
            warnings=warnings,
        )
    return CommitImportResult(entries=normalized, errors=errors, warnings=warnings)


def serialize_commit_entries(entries: Iterable[Mapping[str, Any]]) -> str:
    """Return the final auditable UTF-8 CSV representation (without a BOM)."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["commit_id", "commit_title"])
    for entry in entries:
        writer.writerow([entry["commit"], entry["commit_title"]])
    return output.getvalue()
