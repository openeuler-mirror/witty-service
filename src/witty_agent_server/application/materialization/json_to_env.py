from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from witty_agent_server.application.materialization import converter as conv
from witty_agent_server.application.materialization.core.io_utils import (
    expand_path,
)
from witty_agent_server.application.materialization.spec_json_parser import (
    SCHEMA_VERSION,
)


@dataclass
class JsonConvertOptions:
    template_path: str
    output_path: str = "~/.openclaw/openclaw.json"
    apply_external: bool = True
    verify_recognition: bool = False
    spec_path: str | None = None


def _parse_payload(
    payload_json: str, fallback_spec_path: str | None = None
) -> tuple[dict[str, Any], str]:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("JSON 字符串顶层必须是对象")

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"schema_version 不支持: {schema_version}")

    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("JSON 字符串缺少 spec 对象")

    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta 必须是对象")

    spec_path = meta.get("spec_path") or fallback_spec_path
    if not spec_path:
        raise ValueError("JSON 字符串缺少 meta.spec_path，且未提供 options.spec_path")

    return spec, str(spec_path)


def convert_openclaw_from_json(
    payload_json: str, options: JsonConvertOptions
) -> conv.ConvertReport:
    spec, spec_path = _parse_payload(payload_json, options.spec_path)

    report = conv.ConvertReport()
    output_abs = Path(expand_path(options.output_path))
    existed = output_abs.exists()
    previous = output_abs.read_bytes() if existed else None
    total_phases = 6 if options.verify_recognition else 5

    try:
        conv._ensure_runtime(spec)

        render_options = conv.ConvertOptions(
            spec_path=spec_path,
            template_path=options.template_path,
            output_path=options.output_path,
            apply_external=options.apply_external,
            verify_recognition=options.verify_recognition,
        )

        cfg = conv._render_openclaw_base(spec, render_options)
        openclaw_home = str(output_abs.resolve().parent)

        conv._print_phase(1, total_phases, "写入 openclaw.json 并校验")
        conv._write_config(cfg, options.output_path)
        conv._validate_openclaw(options.output_path)

        conv._print_phase(2, total_phases, "workspace 转换")
        ws_paths = conv._workspace_phase(spec, spec_path, report, openclaw_home)

        conv._print_phase(3, total_phases, "prompt 处理")
        conv._prompt_phase(spec, ws_paths, spec_path, report)

        conv._print_phase(4, total_phases, "skill 转换")
        conv._skills_phase(
            spec,
            ws_paths,
            spec_path,
            openclaw_home,
            options.apply_external,
            report,
        )

        conv._print_phase(5, total_phases, "mcp 转换")
        cfg = conv._mcp_phase(
            spec,
            cfg,
            spec_path,
            openclaw_home,
            options.apply_external,
            report,
        )
        conv._assert_required_fields_resolved(cfg)
        conv._write_config(cfg, options.output_path)
        conv._validate_openclaw(options.output_path)
        if options.verify_recognition:
            conv._print_phase(6, total_phases, "识别校验")
            conv._verify_openclaw_recognition(spec, options.output_path)

        report.updated.append(str(output_abs))
        return report
    except Exception:
        if existed and previous is not None:
            output_abs.parent.mkdir(parents=True, exist_ok=True)
            output_abs.write_bytes(previous)
        elif output_abs.exists():
            output_abs.unlink()
        raise
