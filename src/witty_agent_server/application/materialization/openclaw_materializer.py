from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from witty_agent_server.application.materialization.converter import (
    ConvertOptions,
    convert_openclaw,
)
from witty_agent_server.application.materialization.ports import (
    MaterializeReport,
    SpecMaterializerPort,
)
from witty_agent_server.application.materialization.openclaw_paths import (
    resolve_openclaw_home_dir,
    resolve_openclaw_output_path,
)


_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "openclaw-template.json"
)
logger = logging.getLogger(__name__)


class OpenClawMaterializationError(RuntimeError):
    def __init__(self, message: str, *, spec_path: Path) -> None:
        super().__init__(message)
        self.spec_path = spec_path


class SpecNotFoundError(OpenClawMaterializationError):
    pass


class InvalidOpenClawSpecError(OpenClawMaterializationError):
    pass


@dataclass(slots=True)
class OpenClawSpecMaterializer(SpecMaterializerPort):
    template_path: Path = _TEMPLATE_PATH
    output_path: Path = resolve_openclaw_output_path()
    apply_external: bool = True
    verify_recognition: bool = True

    def __post_init__(self) -> None:
        # 便于测试替换底层转换器，同时保持默认实现简单。
        self._convert_openclaw = convert_openclaw

    def resolve_profile_home(self, profile_name: str | None) -> Path:
        """解析 profile 对应的 OpenClaw home 根目录。"""
        return resolve_openclaw_home_dir(profile_name=profile_name)

    def materialize(
        self,
        spec_path: Path,
        *,
        output_path: Path | None = None,
        profile_name: str | None = None,
    ) -> MaterializeReport:
        resolved_path = spec_path.resolve()
        if not resolved_path.is_file():
            raise SpecNotFoundError(
                f"OpenClaw spec file not found: {resolved_path}",
                spec_path=resolved_path,
            )

        try:
            return self._convert_spec(
                resolved_path,
                output_path=output_path,
                profile_name=profile_name,
            )
        except ValueError as exc:
            raise InvalidOpenClawSpecError(
                f"Invalid OpenClaw spec: {exc}",
                spec_path=resolved_path,
            ) from exc
        except Exception as exc:
            raise OpenClawMaterializationError(
                f"{exc}",
                spec_path=resolved_path,
            ) from exc

    def _convert_spec(
        self,
        spec_path: Path,
        *,
        output_path: Path | None = None,
        profile_name: str | None = None,
    ) -> MaterializeReport:
        resolved_output_path = output_path or resolve_openclaw_output_path(
            profile_name=profile_name
        )
        logger.info(
            "convert openclaw spec: spec_path=%s output_path=%s profile=%s",
            spec_path,
            resolved_output_path,
            profile_name,
        )
        report = self._convert_openclaw(
            ConvertOptions(
                spec_path=str(spec_path),
                template_path=str(self.template_path),
                output_path=str(resolved_output_path),
                apply_external=self.apply_external,
                verify_recognition=self.verify_recognition,
            )
        )
        return MaterializeReport(
            created=list(report.created),
            updated=list(report.updated),
            skipped=list(report.skipped),
            commands=list(report.commands),
        )


_DEFAULT_MATERIALIZER = OpenClawSpecMaterializer()


def materialize(spec_path: Path) -> MaterializeReport:
    return _DEFAULT_MATERIALIZER.materialize(spec_path)


__all__ = [
    "InvalidOpenClawSpecError",
    "MaterializeReport",
    "OpenClawMaterializationError",
    "OpenClawSpecMaterializer",
    "SpecNotFoundError",
    "materialize",
    "resolve_openclaw_output_path",
]
