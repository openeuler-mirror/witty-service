from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from witty_service.application.backport_service import BackportService


def _service(tmp_path) -> BackportService:
    services = SimpleNamespace(
        repository=MagicMock(),
        workspace_store=SimpleNamespace(base_dir=tmp_path),
    )
    return BackportService(services)


def test_clear_model_reference_removes_matching_reference(tmp_path) -> None:
    service = _service(tmp_path)
    config = service.get_config()
    config["backport_model_id"] = "model-1"
    config["target_path"] = "/tmp/target"
    service.update_config(config)

    assert service.clear_model_reference(" model-1 ") is True

    saved = service.get_config()
    assert saved["backport_model_id"] == ""
    assert saved["target_path"] == "/tmp/target"


def test_clear_model_reference_preserves_non_matching_reference(tmp_path) -> None:
    service = _service(tmp_path)
    config = service.get_config()
    config["backport_model_id"] = "model-2"
    service.update_config(config)

    assert service.clear_model_reference("model-1") is False
    assert service.get_config()["backport_model_id"] == "model-2"


def test_clear_model_reference_ignores_empty_model_id(tmp_path) -> None:
    service = _service(tmp_path)

    assert service.clear_model_reference("  ") is False
