from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class MaterializeReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


class SpecMaterializerPort(Protocol):
    def materialize(
        self,
        spec_path: Path,
        *,
        output_path: Path | None = None,
        profile_name: str | None = None,
    ) -> MaterializeReport: ...
