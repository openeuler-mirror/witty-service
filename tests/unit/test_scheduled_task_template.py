from __future__ import annotations

from witty_service.domain.scheduled_task_template import (
    _EN_TEXTS,
    BUILTIN_TEMPLATES,
    localize_scheduled_template,
)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def test_every_builtin_template_has_english_translation() -> None:
    assert set(_EN_TEXTS) == {template.key for template in BUILTIN_TEMPLATES}

    for template in BUILTIN_TEMPLATES:
        localized = localize_scheduled_template(template, "en-US")
        assert localized.key == template.key
        assert not _contains_cjk(localized.name)
        assert not _contains_cjk(localized.description)
        assert not _contains_cjk(localized.content)


def test_localize_keeps_shared_templates_unchanged() -> None:
    template = BUILTIN_TEMPLATES[0]

    localize_scheduled_template(template, "en-US")

    assert template.name == "无人值守系统巡检"
