from __future__ import annotations

import copy
import glob
import json
import logging
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from witty_agent_server.application.materialization.core.io_utils import (
    copy_tree,
    dump_json_atomic,
    ensure_dir,
    expand_path,
    load_yaml,
    read_text,
    upsert_marker_block,
    write_text,
)
from witty_agent_server.application.materialization.core.shell_utils import (
    run_cmd,
)
from witty_agent_server.application.materialization.openclaw_env import (
    build_openclaw_env,
)
from witty_agent_server.application.materialization.openclaw_paths import (
    resolve_openclaw_home_dir,
)


_RESOURCE_ROOT = Path(__file__).resolve().parent / "templates"
logger = logging.getLogger(__name__)


@dataclass
class ConvertOptions:
    spec_path: str
    template_path: str
    output_path: str = "~/.openclaw/openclaw.json"
    apply_external: bool = True
    verify_recognition: bool = False


@dataclass
class ConvertReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def _model_api(provider: str) -> str:
    mapping = {
        "anthropic": "anthropic-messages",
        "openai": "openai-responses",
        "deepseek": "openai-completions",
        "google": "google-generative-ai",
    }
    return mapping.get(provider, "openai-completions")


def _default_base_url(provider: str) -> str:
    mapping = {
        "anthropic": "https://api.anthropic.com/v1",
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "google": "https://generativelanguage.googleapis.com/v1beta",
        "minimax": "https://api.minimaxi.com/v1",
        "MiniMax": "https://api.minimaxi.com/v1",
    }
    return mapping.get(provider, "https://api.openai.com/v1")


def _require_primary(models: list[dict[str, Any]]) -> dict[str, Any]:
    primary = [m for m in models if m.get("is_primary") is True]
    if len(primary) != 1:
        raise ValueError("model 配置必须且只能有一个 is_primary=true")
    return primary[0]


def _parse_mcp_config(raw: str) -> dict[str, Any]:
    obj = yaml.safe_load(raw) if raw else {}
    if not isinstance(obj, dict):
        raise ValueError("mcp.config 解析后必须是对象")
    return obj


def _replace_template_tokens(text: str) -> str:
    token = secrets.token_urlsafe(24)
    return text.replace("${ACCESS_TOKEN}", token)


def _load_existing_gateway_tokens(output_path: str) -> tuple[str | None, str | None]:
    """读取已有配置中的 gateway token，避免重复 materialize 时凭证漂移。"""
    path = Path(expand_path(output_path))
    if not path.exists():
        return None, None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(body, dict):
        return None, None
    gateway = body.get("gateway")
    if not isinstance(gateway, dict):
        return None, None
    auth = gateway.get("auth")
    remote = gateway.get("remote")
    auth_token = auth.get("token") if isinstance(auth, dict) else None
    remote_token = remote.get("token") if isinstance(remote, dict) else None
    return (
        auth_token if isinstance(auth_token, str) and auth_token else None,
        remote_token if isinstance(remote_token, str) and remote_token else None,
    )


def _contains_placeholder(value: str) -> bool:
    return "${" in value and "}" in value


def _assert_required_fields_resolved(cfg: dict[str, Any]) -> None:
    checks: list[tuple[str, Any]] = []
    checks.append(
        (
            "gateway.auth.token",
            (((cfg.get("gateway") or {}).get("auth") or {}).get("token")),
        )
    )
    checks.append(
        (
            "gateway.remote.token",
            (((cfg.get("gateway") or {}).get("remote") or {}).get("token")),
        )
    )

    providers = (cfg.get("models") or {}).get("providers") or {}
    for provider, provider_cfg in providers.items():
        checks.append(
            (f"models.providers.{provider}.apiKey", provider_cfg.get("apiKey"))
        )
        checks.append(
            (f"models.providers.{provider}.baseUrl", provider_cfg.get("baseUrl"))
        )
        for idx, model in enumerate(provider_cfg.get("models") or []):
            checks.append(
                (f"models.providers.{provider}.models[{idx}].id", model.get("id"))
            )
            checks.append(
                (f"models.providers.{provider}.models[{idx}].name", model.get("name"))
            )

    defaults = (cfg.get("agents") or {}).get("defaults") or {}
    model_ref = defaults.get("model")
    if isinstance(model_ref, str):
        checks.append(("agents.defaults.model", model_ref))
    elif isinstance(model_ref, dict):
        checks.append(("agents.defaults.model.primary", model_ref.get("primary")))
        for idx, fb in enumerate(model_ref.get("fallbacks") or []):
            checks.append((f"agents.defaults.model.fallbacks[{idx}]", fb))

    unresolved = [
        k for k, v in checks if isinstance(v, str) and _contains_placeholder(v)
    ]
    if unresolved:
        raise ValueError(f"存在未实值化占位符字段: {', '.join(unresolved)}")


def _workspace_map(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return spec.get("workspace") or {"default": {}}


def _subagents(spec: dict[str, Any]) -> list[dict[str, Any]]:
    raw = spec.get("subAgents")
    if raw is None:
        raw = spec.get("subagents")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("subAgents/subagents 配置必须是数组")
    return raw


def _resolve_workspace_ref(
    expr: str | None,
    workspaces: dict[str, dict[str, Any]],
    *,
    openclaw_home: str,
) -> str:
    if not expr:
        default_workspace = workspaces.get("default", {}).get("path")
        if not isinstance(default_workspace, str) or not default_workspace:
            default_workspace = f"{openclaw_home}/workspace"
        return expand_path(default_workspace)
    if expr.startswith("${workspace.") and expr.endswith("}"):
        key = expr[len("${workspace.") : -1]
        if key not in workspaces:
            raise ValueError(f"subAgent workspace 引用不存在: {expr}")
        workspace_path = workspaces[key].get("path")
        if not isinstance(workspace_path, str) or not workspace_path:
            workspace_path = f"{openclaw_home}/workspace-{key}"
        return expand_path(workspace_path)
    return expand_path(expr)


def _spec_base_dir(spec_path: str) -> str:
    return str(Path(spec_path).resolve().parent)


def _build_models(
    spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    models = spec.get("model") or []
    if not models:
        raise ValueError("agent-spec.yaml 缺少 model 配置")

    primary = _require_primary(models)
    providers: dict[str, Any] = {}
    aliases: dict[str, Any] = {}

    for m in models:
        provider = m["provider"]
        name = m["name"]
        reasoning = m.get("reasoning", m.get("resaoning", False))
        model_item = {
            "id": name,
            "name": name,
            "reasoning": bool(reasoning),
            "contextWindow": m.get("contextWindow", 128000),
            "maxTokens": m.get("max_tokens", m.get("maxTokens", 8192)),
            "input": m.get("input", ["text"]),
        }
        if provider not in providers:
            providers[provider] = {
                "apiKey": m.get("apiKey", ""),
                "baseUrl": m.get("baseUrl") or _default_base_url(provider),
                "api": _model_api(provider),
                "models": [],
            }
        providers[provider]["models"].append(model_item)
        aliases[f"{provider}/{name}"] = {"alias": name}

    primary_ref = f"{primary['provider']}/{primary['name']}"
    fallbacks = [
        f"{m['provider']}/{m['name']}" for m in models if not m.get("is_primary")
    ]

    default_model: dict[str, Any] = {"primary": primary_ref}
    if fallbacks:
        default_model["fallbacks"] = fallbacks

    return {"mode": "merge", "providers": providers}, default_model, aliases


def _render_openclaw_base(
    spec: dict[str, Any], options: ConvertOptions
) -> dict[str, Any]:
    template_text = read_text(options.template_path)
    template_text = _replace_template_tokens(template_text)
    cfg = json.loads(template_text)
    existing_auth_token, existing_remote_token = _load_existing_gateway_tokens(
        options.output_path
    )

    models_cfg, default_model, aliases = _build_models(spec)
    cfg["models"] = models_cfg
    gateway = cfg.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise ValueError("gateway 配置必须是对象")
    if existing_auth_token:
        gateway.setdefault("auth", {})
        if not isinstance(gateway["auth"], dict):
            raise ValueError("gateway.auth 配置必须是对象")
        gateway["auth"]["token"] = existing_auth_token
    if existing_remote_token:
        gateway.setdefault("remote", {})
        if not isinstance(gateway["remote"], dict):
            raise ValueError("gateway.remote 配置必须是对象")
        gateway["remote"]["token"] = existing_remote_token

    openclaw_home = str(Path(expand_path(options.output_path)).resolve().parent)
    workspaces = _workspace_map(spec)
    default_ws = _resolve_workspace_ref(
        workspaces.get("default", {}).get("path"),
        workspaces,
        openclaw_home=openclaw_home,
    )

    cfg.setdefault("agents", {}).setdefault("defaults", {})
    cfg["agents"]["defaults"]["workspace"] = default_ws
    cfg["agents"]["defaults"]["model"] = default_model
    cfg["agents"]["defaults"]["models"] = aliases

    tools = spec.get("tools") or {}
    if tools:
        cfg.setdefault("tools", {})
        if tools.get("profile"):
            cfg["tools"]["profile"] = tools["profile"]
        if tools.get("allowed"):
            cfg["tools"]["allow"] = tools["allowed"]
        if tools.get("deny"):
            cfg["tools"]["deny"] = tools["deny"]

    subagents = _subagents(spec)
    if subagents:
        # Drop legacy single-agent placeholders from template in subAgents mode.
        cfg.pop("agent", None)
        cfg.pop("agent.default", None)
        cfg.setdefault("agents", {})
        cfg["agents"].pop("default", None)
        cfg["agents"].pop("agent.default", None)
        cfg["agents"].pop("defaults", None)
        lst = []
        for s in subagents:
            model = s.get("model")
            if model is None:
                model = copy.deepcopy(default_model)
            item = {
                "id": s["id"],
                "workspace": _resolve_workspace_ref(
                    s.get("workspace"),
                    workspaces,
                    openclaw_home=openclaw_home,
                ),
                "model": model,
                "skills": [
                    x["name"] if isinstance(x, dict) and "name" in x else x
                    for x in (s.get("skills") or [])
                ],
            }
            lst.append(item)
        default_idx = 0
        for idx, item in enumerate(lst):
            if item["id"] == "main":
                default_idx = idx
                break
        lst[default_idx]["default"] = True
        cfg["agents"]["list"] = lst
        # Defensive cleanup for legacy templates that may still
        # carry dotted placeholders.
        cfg.pop("agent", None)
        cfg.pop("agent.default", None)
        (cfg.get("agents") or {}).pop("default", None)
        (cfg.get("agents") or {}).pop("agent.default", None)
        (cfg.get("agents") or {}).pop("defaults", None)

    return cfg


def _validate_openclaw(output_path: str) -> None:
    state_dir = str(Path(expand_path(output_path)).resolve().parent)
    run_cmd(
        ["openclaw", "config", "validate"],
        check=True,
        env=build_openclaw_env(state_dir=state_dir),
    )


def _parse_json_stdout(cmd_name: str, stdout: str) -> Any:
    try:
        return json.loads(stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{cmd_name} 输出不是合法 JSON: {exc}") from exc


def _expected_mcp_names(spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in spec.get("mcp") or []:
        name = item.get("name")
        if not name and item.get("config"):
            cfg = _parse_mcp_config(item["config"])
            name = cfg.get("name")
        if name:
            names.append(str(name))
    return names


def _verify_openclaw_recognition(spec: dict[str, Any], output_path: str) -> None:
    state_dir = str(Path(expand_path(output_path)).resolve().parent)
    env = build_openclaw_env(state_dir=state_dir)
    subagents = _subagents(spec)

    # base parseability/readability in active state dir
    run_cmd(["openclaw", "config", "validate", "--json"], check=True, env=env)
    if subagents:
        res = run_cmd(
            ["openclaw", "config", "get", "agents.list", "--json"], check=True, env=env
        )
        agents = _parse_json_stdout(
            "openclaw config get agents.list --json", res.stdout
        )
        if not isinstance(agents, list):
            raise RuntimeError("openclaw 未识别 agents.list 为数组")
        actual_ids = {
            str(x.get("id")) for x in agents if isinstance(x, dict) and x.get("id")
        }
        expected_ids = {
            str(x["id"]) for x in subagents if isinstance(x, dict) and x.get("id")
        }
        missing = sorted(expected_ids - actual_ids)
        if missing:
            raise RuntimeError(f"openclaw 未识别到 subAgents: {', '.join(missing)}")
    else:
        run_cmd(
            ["openclaw", "config", "get", "agents.defaults.model", "--json"],
            check=True,
            env=env,
        )

    tools = spec.get("tools") or {}
    if tools:
        run_cmd(["openclaw", "config", "get", "tools", "--json"], check=True, env=env)

    expected_mcp = _expected_mcp_names(spec)
    if expected_mcp:
        mcp_list = run_cmd(["openclaw", "mcp", "list", "--json"], check=True, env=env)
        listed = _parse_json_stdout("openclaw mcp list --json", mcp_list.stdout)
        listed_names: set[str] = set()
        if isinstance(listed, list):
            for item in listed:
                if isinstance(item, dict) and item.get("name"):
                    listed_names.add(str(item["name"]))
                elif isinstance(item, str):
                    listed_names.add(item)
        elif isinstance(listed, dict):
            listed_names = {str(k) for k in listed.keys()}
        missing_mcp = sorted(set(expected_mcp) - listed_names)
        if missing_mcp:
            raise RuntimeError(f"openclaw mcp 未识别到: {', '.join(missing_mcp)}")
        for name in expected_mcp:
            run_cmd(["openclaw", "mcp", "show", name, "--json"], check=True, env=env)

    # Skills are loaded by workspace/shared dirs; check readiness explicitly.
    run_cmd(["openclaw", "skills", "list", "--eligible", "--json"], check=True, env=env)
    run_cmd(["openclaw", "skills", "check", "--json"], check=True, env=env)


def _write_config(cfg: dict[str, Any], output_path: str) -> None:
    dump_json_atomic(expand_path(output_path), cfg)


def _resolve_source(base_dir: str, rel: str) -> str:
    p = Path(rel)
    if p.is_absolute():
        return str(p)
    return str((Path(base_dir) / rel).resolve())


def _workspace_phase(
    spec: dict[str, Any],
    spec_path: str,
    report: ConvertReport,
    openclaw_home: str,
) -> dict[str, str]:
    workspaces = _workspace_map(spec)

    ws_paths: dict[str, str] = {}
    for name, data in workspaces.items():
        raw_path = data.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raw_path = f"{openclaw_home}/workspace"
            if name != "default":
                raw_path = f"{openclaw_home}/workspace-{name}"
        ws_paths[name] = expand_path(raw_path)

    for name, ws in workspaces.items():
        path = ws_paths[name]
        ensure_dir(path)

        base_dir = _spec_base_dir(spec_path)
        agents_src = ws.get("AGENTS")
        soul_src = ws.get("SOUL")
        if agents_src:
            agents_abs = _resolve_source(base_dir, agents_src)
            if Path(agents_abs).exists():
                content = read_text(agents_abs)
                write_text(str(Path(path) / "AGENTS.md"), content)
                report.updated.append(str(Path(path) / "AGENTS.md"))
            else:
                raise FileNotFoundError(
                    f"workspace[{name}] AGENTS 源文件不存在: {agents_abs}"
                )
        if soul_src:
            soul_abs = _resolve_source(base_dir, soul_src)
            if Path(soul_abs).exists():
                content = read_text(soul_abs)
                write_text(str(Path(path) / "SOUL.md"), content)
                report.updated.append(str(Path(path) / "SOUL.md"))
            else:
                raise FileNotFoundError(
                    f"workspace[{name}] SOUL 源文件不存在: {soul_abs}"
                )

    return ws_paths


def _prompt_phase(
    spec: dict[str, Any],
    ws_paths: dict[str, str],
    spec_path: str,
    report: ConvertReport,
) -> None:
    prompt = spec.get("prompt") or {}
    chunks: list[tuple[str, str]] = []

    if prompt.get("system"):
        chunks.append(("inline(prompt.system)", prompt["system"].rstrip()))

    base = _spec_base_dir(spec_path)
    for pattern in sorted(prompt.get("system_file") or []):
        abs_pattern = _resolve_source(base, pattern)
        for p in sorted(glob.glob(abs_pattern)):
            chunks.append(
                (
                    f"file({Path(p).relative_to(base) if p.startswith(base) else p})",
                    read_text(p).rstrip(),
                )
            )

    if not chunks:
        return

    lines = ["## Imported System Prompt", ""]
    for src, text in chunks:
        lines.append(f"### Source: {src}")
        lines.append(text)
        lines.append("")
    body = "\n".join(lines).rstrip()

    begin = "<!-- BEGIN: IMPORTED_SYSTEM_PROMPT -->"
    end = "<!-- END: IMPORTED_SYSTEM_PROMPT -->"

    default_agents = str(Path(ws_paths["default"]) / "AGENTS.md")
    old = read_text(default_agents) if Path(default_agents).exists() else ""
    new_text = upsert_marker_block(old, begin, end, body)
    write_text(default_agents, new_text)
    report.updated.append(default_agents)


def _install_or_materialize_skill(
    entry: dict[str, Any],
    workdir: str,
    spec_path: str,
    apply_external: bool,
    report: ConvertReport,
) -> None:
    base = _spec_base_dir(spec_path)
    skills_dir = str(Path(workdir) / "skills")
    ensure_dir(skills_dir)

    name = entry.get("name")
    mode = entry.get("installed")
    logger.debug(
        "_install_or_materialize_skill: name=%s mode=%s workdir=%s",
        name,
        mode,
        workdir,
    )
    if not name:
        raise ValueError("skill 缺少 name")

    if mode == "local_link":
        src = _resolve_source(base, entry["source"])
        dst = str(Path(skills_dir) / name)
        logger.debug("local_link: src=%s dst=%s", src, dst)
        if Path(dst).exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        report.updated.append(dst)
        return

    if mode == "local_created":
        dst = str(Path(skills_dir) / name / "SKILL.md")
        logger.debug("local_created: dst=%s", dst)
        write_text(dst, entry.get("inline", ""))
        report.updated.append(dst)
        return

    if mode == "tool_installed":
        if not name:
            raise ValueError("skill tool_installed 缺少 name")
        cmd = ["clawhub", "install", name, "--workdir", workdir, "--dir", "skills"]
        logger.debug("tool_installed: running command: %s", " ".join(cmd))
        report.commands.append(" ".join(cmd))
        if not apply_external:
            raise RuntimeError("tool_installed skill 不允许跳过外部命令")
        try:
            run_cmd(cmd, check=True)
        except RuntimeError as exc:
            logger.warning("tool_installed first attempt failed: %s", exc)
            if "--force" not in str(exc):
                raise
            force_cmd = [*cmd, "--force"]
            report.commands.append(" ".join(force_cmd))
            try:
                run_cmd(force_cmd, check=True)
            except RuntimeError as exc:
                raise RuntimeError(f"clawhub install {name} skill faild") from exc
        return

def _skills_phase(
    spec: dict[str, Any],
    _ws_paths: dict[str, str],
    spec_path: str,
    openclaw_home: str,
    apply_external: bool,
    report: ConvertReport,
) -> None:
    logger.debug("_skills_phase started: apply_external=%s", apply_external)
    global_skills = spec.get("skills") or []
    logger.debug("global_skills count=%s", len(global_skills))
    for idx, entry in enumerate(global_skills):
        logger.debug("processing global_skills[%s]: %s", idx, entry)
        _install_or_materialize_skill(
            entry, openclaw_home, spec_path, apply_external, report
        )
    logger.debug("global_skills done")

    subagents = _subagents(spec)
    logger.debug("subagents count=%s", len(subagents))
    for sub_idx, sub in enumerate(subagents):
        ws = _resolve_workspace_ref(
            sub.get("workspace"),
            _workspace_map(spec),
            openclaw_home=openclaw_home,
        )
        sub_skills = sub.get("skills") or []
        logger.debug(
            "subagents[%s] id=%s ws=%s skills count=%s",
            sub_idx,
            sub.get("id"),
            ws,
            len(sub_skills),
        )
        for skill_idx, entry in enumerate(sub_skills):
            logger.debug(
                "processing subagents[%s].skills[%s]: %s",
                sub_idx,
                skill_idx,
                entry,
            )
            if isinstance(entry, str):
                entry = {"name": entry, "installed": "tool_installed"}
            _install_or_materialize_skill(entry, ws, spec_path, apply_external, report)
    logger.debug("_skills_phase completed")


def _normalize_mcp_server(
    obj: dict[str, Any],
    source_dir: str | None,
    name_hint: str | None = None,
    *,
    openclaw_home: str | None = None,
) -> tuple[str, dict[str, Any]]:
    name = obj.get("name") or name_hint
    if not name:
        raise ValueError("mcp 项缺少 name")

    server: dict[str, Any] = {}
    if obj.get("command"):
        server["command"] = obj["command"]
    if obj.get("args"):
        args = list(obj["args"])
        if source_dir:
            base = Path(openclaw_home or str(resolve_openclaw_home_dir())) / "mcp" / name
            normalized = []
            for a in args:
                pa = Path(str(a))
                if pa.is_absolute() and pa.name:
                    normalized.append(str(base / pa.name))
                else:
                    normalized.append(str(a))
            args = normalized
        server["args"] = args
    if obj.get("env"):
        server["env"] = obj["env"]
    if obj.get("cwd"):
        server["cwd"] = obj["cwd"]
    if obj.get("workingDirectory"):
        server["workingDirectory"] = obj["workingDirectory"]
    if obj.get("url"):
        server["url"] = obj["url"]
    headers = copy.deepcopy(obj.get("headers") or {})
    api_key = obj.get("apiKey")
    if api_key and "Authorization" not in headers:
        if (
            isinstance(api_key, dict)
            and api_key.get("source") == "env"
            and api_key.get("id")
        ):
            headers["Authorization"] = f"Bearer ${{{api_key['id']}}}"
        elif isinstance(api_key, str):
            headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        server["headers"] = headers
    return name, server


def _mcp_phase(
    spec: dict[str, Any],
    cfg: dict[str, Any],
    spec_path: str,
    openclaw_home: str,
    apply_external: bool,
    report: ConvertReport,
) -> dict[str, Any]:
    cfg.setdefault("mcp", {}).setdefault("servers", {})
    base_dir = _spec_base_dir(spec_path)

    mcp_items = spec.get("mcp") or []
    logger.debug(
        "_mcp_phase started: apply_external=%s mcp items count=%s",
        apply_external,
        len(mcp_items),
    )

    for idx, item in enumerate(mcp_items):
        mode = item.get("installed")
        name = item.get("name") or "unknown"
        logger.debug(
            "_mcp_phase processing item[%s]: name=%s mode=%s",
            idx,
            name,
            mode,
        )

        if mode in {"local_link", "remote_link"}:
            obj = _parse_mcp_config(item.get("config", ""))
            source_dir = None
            if mode == "local_link":
                src = _resolve_source(base_dir, item["source"])
                name = obj.get("name") or item.get("name")
                if not name:
                    raise ValueError("local_link mcp 缺少 name")
                dst = str(Path(openclaw_home) / "mcp" / name)
                if Path(src).exists():
                    ensure_dir(dst)
                    copy_tree(src, dst)
                    report.updated.append(dst)
                    source_dir = dst
                else:
                    raise FileNotFoundError(f"mcp[{name}] source 不存在: {src}")
            name, server = _normalize_mcp_server(
                obj,
                source_dir,
                item.get("name"),
                openclaw_home=openclaw_home,
            )
            cfg["mcp"]["servers"][name] = server
            continue

        if mode == "tool_installed":
            name = item.get("name")
            if not name:
                raise ValueError("tool_installed mcp 缺少 name")
            if not apply_external:
                raise RuntimeError("tool_installed mcp 不允许跳过外部命令")

            extra_args = item.get("args") or []
            if not isinstance(extra_args, list):
                raise ValueError("mcp tool_installed args 必须是数组")

            # 按规则优先支持 add/import，再通过 get 回填到 openclaw.json
            if item.get("config"):
                cfg_obj = _parse_mcp_config(item["config"])
                add_cmd: list[str] = ["mcporter", "config", "add", name]
                transport = cfg_obj.get("transport")
                url = cfg_obj.get("url")
                command = cfg_obj.get("command")
                args = cfg_obj.get("args") or []
                env = cfg_obj.get("env") or {}
                headers = cfg_obj.get("headers") or {}

                if url:
                    add_cmd.append(str(url))
                elif command:
                    add_cmd.extend(["--command", str(command)])
                if transport:
                    add_cmd.extend(["--transport", str(transport)])
                for a in args:
                    add_cmd.extend(["--arg", str(a)])
                for k, v in env.items():
                    add_cmd.extend(["--env", f"{k}={v}"])
                for k, v in headers.items():
                    add_cmd.extend(["--header", f"{k}={v}"])
                for a in extra_args:
                    add_cmd.append(str(a))

                report.commands.append(" ".join(add_cmd))
                run_cmd(add_cmd, check=True)
            elif extra_args:
                add_cmd = ["mcporter", "config", "add", name]
                for a in extra_args:
                    add_cmd.append(str(a))
                report.commands.append(" ".join(add_cmd))
                run_cmd(add_cmd, check=True)

            if item.get("import"):
                import_value = item.get("import")
                if isinstance(import_value, str):
                    kind = import_value
                    import_cmd = [
                        "mcporter",
                        "config",
                        "import",
                        kind,
                        "--filter",
                        name,
                        "--copy",
                    ]
                elif isinstance(import_value, dict):
                    kind = import_value.get("kind")
                    if not kind:
                        raise ValueError("mcp tool_installed import 缺少 kind")
                    import_cmd = ["mcporter", "config", "import", str(kind), "--copy"]
                    if import_value.get("filter"):
                        import_cmd.extend(["--filter", str(import_value["filter"])])
                    if import_value.get("path"):
                        import_cmd.extend(["--path", str(import_value["path"])])
                else:
                    raise ValueError("mcp tool_installed import 配置格式错误")
                report.commands.append(" ".join(import_cmd))
                run_cmd(import_cmd, check=True)

            cmd = ["mcporter", "config", "get", name, "--json"]
            report.commands.append(" ".join(cmd))
            res = run_cmd(cmd, check=False)
            if res.code != 0:
                # 尝试最小导入占位，给出明确错误
                raise RuntimeError(
                    "mcporter 未找到 server: "
                    f"{name}. 请先通过 mcporter config add/import 配置"
                )
            payload = json.loads(res.stdout)
            if not isinstance(payload, dict):
                raise RuntimeError(f"mcporter 返回异常: {name}")
            _, server = _normalize_mcp_server(
                payload,
                None,
                name,
                openclaw_home=openclaw_home,
            )
            cfg["mcp"]["servers"][name] = server
            continue

    return cfg


def _ensure_runtime(spec: dict[str, Any]) -> None:
    rt = spec.get("runtimeType", "openclaw")
    if rt != "openclaw":
        raise ValueError(f"runtimeType 必须是 openclaw，当前为: {rt}")


def _print_phase(idx: int, total: int, title: str) -> None:
    logger.info("[%s/%s] %s", idx, total, title)


def convert_openclaw(options: ConvertOptions) -> ConvertReport:
    report = ConvertReport()
    output_abs = Path(expand_path(options.output_path))
    openclaw_home = str(output_abs.resolve().parent)
    existed = output_abs.exists()
    previous = output_abs.read_bytes() if existed else None
    total_phases = 6 if options.verify_recognition else 5

    try:
        spec = load_yaml(options.spec_path)
        _ensure_runtime(spec)

        cfg = _render_openclaw_base(spec, options)

        # 1) write + validate
        _print_phase(1, total_phases, "写入 openclaw.json 并校验")
        _write_config(cfg, options.output_path)
        _validate_openclaw(options.output_path)

        # 2) workspace
        _print_phase(2, total_phases, "workspace 转换")
        ws_paths = _workspace_phase(
            spec,
            options.spec_path,
            report,
            openclaw_home,
        )

        # 3) prompt
        _print_phase(3, total_phases, "prompt 处理")
        _prompt_phase(spec, ws_paths, options.spec_path, report)

        # 4) skills
        _print_phase(4, total_phases, "skill 转换")
        _skills_phase(
            spec,
            ws_paths,
            options.spec_path,
            openclaw_home,
            options.apply_external,
            report,
        )

        # 5) mcp
        _print_phase(5, total_phases, "mcp 转换")
        cfg = _mcp_phase(
            spec,
            cfg,
            options.spec_path,
            openclaw_home,
            options.apply_external,
            report,
        )
        _assert_required_fields_resolved(cfg)
        _write_config(cfg, options.output_path)
        _validate_openclaw(options.output_path)
        if options.verify_recognition:
            _print_phase(6, total_phases, "识别校验")
            _verify_openclaw_recognition(spec, options.output_path)

        report.updated.append(str(output_abs))
        logger.info("convert_openclaw completed successfully")
        return report
    except Exception as exc:
        import traceback
        logger.error("convert_openclaw failed with error: %s", exc)
        logger.debug("Traceback:\n%s", traceback.format_exc())
        # 失败回滚：恢复旧文件；若原本不存在则删除新文件
        if existed and previous is not None:
            output_abs.parent.mkdir(parents=True, exist_ok=True)
            output_abs.write_bytes(previous)
        elif output_abs.exists():
            output_abs.unlink()
        raise
