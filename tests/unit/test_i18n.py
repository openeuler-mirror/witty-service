from __future__ import annotations

from witty_service.i18n import DEFAULT_LOCALE, normalize_locale


def test_missing_or_empty_header_falls_back_to_default() -> None:
    assert normalize_locale(None) == DEFAULT_LOCALE
    assert normalize_locale("") == DEFAULT_LOCALE
    assert normalize_locale("   ") == DEFAULT_LOCALE


def test_primary_subtag_maps_to_supported_locale() -> None:
    assert normalize_locale("en") == "en-US"
    assert normalize_locale("en-GB") == "en-US"
    assert normalize_locale("zh") == "zh-CN"
    assert normalize_locale("zh-TW") == "zh-CN"
    assert normalize_locale("EN-us") == "en-US"


def test_quality_order_wins() -> None:
    assert normalize_locale("zh-CN;q=0.4,en-US;q=0.9") == "en-US"
    assert normalize_locale("en-US,en;q=0.9") == "en-US"
    assert normalize_locale("zh-CN,en-US;q=0.9") == "zh-CN"


def test_zero_quality_candidate_is_ignored() -> None:
    assert normalize_locale("en;q=0,zh-CN") == "zh-CN"
    assert normalize_locale("en;q=0") == DEFAULT_LOCALE


def test_invalid_quality_and_wildcard_are_skipped() -> None:
    assert normalize_locale("en;q=abc,zh-CN;q=0.5") == "zh-CN"
    assert normalize_locale("*,en;q=0.8") == "en-US"


def test_quality_is_clamped_to_unit_interval() -> None:
    assert normalize_locale("en;q=5,zh-CN;q=2") == "en-US"
    assert normalize_locale("en;q=-1,zh-CN;q=0.3") == "zh-CN"


def test_unknown_language_falls_back_to_default() -> None:
    assert normalize_locale("fr-FR,de;q=0.8") == DEFAULT_LOCALE
    assert normalize_locale("enigma") == DEFAULT_LOCALE
