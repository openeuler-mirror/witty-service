from __future__ import annotations

import pytest

import witty_service.config as config_module


def _clear_insight_env(monkeypatch) -> None:
    for key in (
        "WITTY_INSIGHT_ENABLED",
        "WITTY_INSIGHT_BASE_URL",
        "WITTY_INSIGHT_TIMEOUT_SECONDS",
        "WITTY_INSIGHT_BEARER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def _clear_database_env(monkeypatch) -> None:
    for key in ("WITTY_DATABASE_URL", "WITTY_DATABASE_AUTO_CREATE"):
        monkeypatch.delenv(key, raising=False)


def test_database_settings_defaults_enable_auto_create(monkeypatch) -> None:
    _clear_database_env(monkeypatch)

    settings = config_module.DatabaseSettings.from_env()

    assert settings.url.endswith("/.witty/db/witty_service.sqlite3")
    assert settings.auto_create is True


@pytest.mark.parametrize("raw_value", ["1", "true", "TRUE", "yes"])
def test_database_settings_enables_auto_create(raw_value, monkeypatch) -> None:
    monkeypatch.setenv("WITTY_DATABASE_URL", "sqlite:////tmp/witty.sqlite3")
    monkeypatch.setenv("WITTY_DATABASE_AUTO_CREATE", raw_value)

    settings = config_module.DatabaseSettings.from_env()

    assert settings.url == "sqlite:////tmp/witty.sqlite3"
    assert settings.auto_create is True


@pytest.mark.parametrize("raw_value", ["0", "false", "no", "unexpected"])
def test_database_settings_disables_auto_create(raw_value, monkeypatch) -> None:
    monkeypatch.setenv("WITTY_DATABASE_AUTO_CREATE", raw_value)

    settings = config_module.DatabaseSettings.from_env()

    assert settings.auto_create is False


def test_insight_settings_defaults(monkeypatch) -> None:
    _clear_insight_env(monkeypatch)

    settings = config_module.InsightSettings.from_env()

    assert settings.enabled is True
    assert settings.base_url == "http://127.0.0.1:7396"
    assert settings.timeout_seconds == 10.0
    assert settings.bearer_token is None


def test_scheduler_settings_default_timezone(monkeypatch) -> None:
    monkeypatch.delenv("WITTY_SCHEDULER_TZ", raising=False)

    assert config_module.SchedulerSettings.from_env().timezone == "Asia/Shanghai"


def test_scheduler_settings_accepts_valid_timezone(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_SCHEDULER_TZ", "America/New_York")

    assert config_module.SchedulerSettings.from_env().timezone == "America/New_York"


def test_scheduler_settings_rejects_invalid_timezone(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_SCHEDULER_TZ", "Invalid/Zone")

    with pytest.raises(
        ValueError, match="Invalid timezone configured via WITTY_SCHEDULER_TZ"
    ):
        config_module.SchedulerSettings.from_env()


def test_insight_settings_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_INSIGHT_ENABLED", "true")
    monkeypatch.setenv("WITTY_INSIGHT_BASE_URL", "http://10.0.0.8:7396")
    monkeypatch.setenv("WITTY_INSIGHT_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("WITTY_INSIGHT_BEARER_TOKEN", "secret-token")

    settings = config_module.InsightSettings.from_env()

    assert settings.enabled is True
    assert settings.base_url == "http://10.0.0.8:7396"
    assert settings.timeout_seconds == 3.5
    assert settings.bearer_token == "secret-token"


def test_insight_settings_can_be_explicitly_disabled(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_INSIGHT_ENABLED", "false")

    settings = config_module.InsightSettings.from_env()

    assert settings.enabled is False


def test_insight_settings_normalizes_blank_token_to_none(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_INSIGHT_BEARER_TOKEN", "   ")

    settings = config_module.InsightSettings.from_env()

    assert settings.bearer_token is None


def test_settings_from_env_includes_insight_settings(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_INSIGHT_ENABLED", "true")
    monkeypatch.setenv("WITTY_INSIGHT_BASE_URL", "http://insight.internal:7396")

    settings = config_module.Settings.from_env()

    assert settings.insight.enabled is True
    assert settings.insight.base_url == "http://insight.internal:7396"
