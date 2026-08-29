import pytest

from witty_service.application.backport_commit_import import (
    MAX_COMMIT_IMPORT_BYTES,
    parse_commit_import,
    serialize_commit_entries,
    validate_commit_entries,
)


@pytest.mark.parametrize(
    "commit_header",
    ["commit hash", "commit", "hash", "sha", "commit_id"],
)
@pytest.mark.parametrize(
    "title_header",
    ["commit title", "title", "subject", "patch title"],
)
def test_parse_csv_supports_legacy_header_aliases(
    commit_header: str, title_header: str
) -> None:
    result = parse_commit_import(
        f"{commit_header},{title_header}\nabcdef1,first title\n".encode(),
        delimiter="csv",
    )

    assert result.errors == []
    assert result.entries == [{"commit": "abcdef1", "commit_title": "first title"}]


def test_parse_tsv_maps_reversed_legacy_headers_to_canonical_fields() -> None:
    result = parse_commit_import(
        b"subject\tsha\nfirst title\tabcdef1\n",
        delimiter="tsv",
    )

    assert result.errors == []
    assert result.entries == [{"commit": "abcdef1", "commit_title": "first title"}]
    assert result.rows == [
        {"row": 2, "commit": "abcdef1", "commit_title": "first title"}
    ]


def test_parse_normalizes_header_case_whitespace_and_separators() -> None:
    result = parse_commit_import(
        b" Commit-Hash ,PATCH_TITLE\nabcdef1,first title\n",
        delimiter="csv",
    )

    assert result.errors == []
    assert result.entries == [{"commit": "abcdef1", "commit_title": "first title"}]


def test_parse_csv_supports_utf8_bom_header_and_quoted_titles() -> None:
    result = parse_commit_import(
        '\ufeffcommit_id,commit_title\nabcdef1,"fix: comma, in title"\n'.encode(),
        delimiter="csv",
    )

    assert result.errors == []
    assert result.entries == [
        {"commit": "abcdef1", "commit_title": "fix: comma, in title"}
    ]


def test_parse_tsv_without_header_deduplicates_exact_entries() -> None:
    result = parse_commit_import(
        b"ABCDEF1\tfirst title\nabcdef1\tfirst title\n",
        delimiter="tsv",
    )

    assert result.entries == [{"commit": "abcdef1", "commit_title": "first title"}]
    assert result.warnings == [
        {"row": 2, "message": "与第 1 行重复，已保留首次出现的提交。"}
    ]


def test_parse_rejects_invalid_columns_and_conflicting_titles() -> None:
    result = parse_commit_import(
        b"abcdef1,first\nabcdef1,second\n1234567,too,many\n",
        delimiter="csv",
    )

    assert result.entries == [{"commit": "abcdef1", "commit_title": "first"}]
    assert result.errors == [
        {
            "row": 3,
            "field": "row",
            "message": "每行必须恰好包含 commit_id 和 commit_title 两列。",
        },
        {
            "row": 2,
            "field": "commit_title",
            "message": "与第 1 行使用相同 commit_id 但 commit_title 不同。",
        },
    ]
    assert result.rows == [
        {"row": 1, "commit": "abcdef1", "commit_title": "first"},
        {"row": 2, "commit": "abcdef1", "commit_title": "second"},
        {"row": 3, "commit": "1234567", "commit_title": "too"},
    ]


def test_validate_rejects_multiline_title_and_limits() -> None:
    result = validate_commit_entries(
        [{"commit": "abcdef1", "commit_title": "one\ntwo"}]
    )
    oversized = parse_commit_import(
        b"x" * (MAX_COMMIT_IMPORT_BYTES + 1), delimiter="csv"
    )

    assert result.entries == []
    assert result.errors[0]["field"] == "commit_title"
    assert oversized.entries == []
    assert oversized.errors[0]["field"] == "file"


@pytest.mark.parametrize("entry", [1, "abcdef1", [], (1,), ("1", {})])
def test_validate_rejects_non_object_or_malformed_internal_entry(entry: object) -> None:
    result = validate_commit_entries([entry])  # type: ignore[list-item]

    assert result.entries == []
    assert result.errors == [
        {
            "row": 1,
            "field": "entry",
            "message": "提交条目必须是对象，且包含 commit 与 commit_title。",
        }
    ]


def test_validate_rejects_final_entries_larger_than_one_mib() -> None:
    result = validate_commit_entries(
        [{"commit": "abcdef1", "commit_title": "x" * MAX_COMMIT_IMPORT_BYTES}]
    )

    assert result.entries == []
    assert result.errors == [
        {"field": "entries", "message": "提交清单不能超过 1 MiB。"}
    ]


def test_serialize_uses_canonical_header_and_csv_quoting() -> None:
    assert (
        serialize_commit_entries(
            [{"commit": "abcdef1", "commit_title": "title, with comma"}]
        )
        == 'commit_id,commit_title\r\nabcdef1,"title, with comma"\r\n'
    )
