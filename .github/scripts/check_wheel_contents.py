#!/usr/bin/env python3
"""检查 wheel 产物是否混入了已删除的 Runtime/Sandbox 模块或依赖。

背景：本仓库已裁剪为仅支持 OpenCode 单 runtime，删除过 OpenClaw、DSH、
E2B 相关模块与 ``deepseek-harness-sdk`` 依赖。Setuptools 的增量构建
（``build/lib``）不会自动清理已删除源码的旧文件，这些陈旧文件会被原样
打进 wheel，导致发布产物与源码树不一致。

检查项：

1. wheel 中每个 ``witty_service`` / ``witty_agent_server`` 的 ``.py`` 模块，
   都必须在 ``--src-root`` 下存在同名源码文件。
2. 显式禁止的已删除模块（OpenClaw / DSH / materialization / E2B）不得出现。
3. ``METADATA`` 的 ``Requires-Dist`` 不得包含已删除的依赖。

用法::

    python .github/scripts/check_wheel_contents.py dist/*.whl --src-root src

任一项不通过时以非零状态码退出，并在标准输出列出全部问题。
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path

PACKAGE_ROOTS = ("witty_service", "witty_agent_server")

# 已删除的包目录/文件：只要出现在 wheel 里就失败。
# 注意：``witty_service/application/awesome_openclaw_sync.py`` 是仍在使用的
# Skill 内容同步模块，名称里带 openclaw 但必须保留，不要加入此列表。
FORBIDDEN_PREFIXES = ("witty_agent_server/application/materialization/",)

FORBIDDEN_GLOBS = (
    "witty_agent_server/adapters/openclaw_adapter.py",
    "witty_agent_server/application/runtime_bundle.py",
    "witty_agent_server/application/services/agent/dsh_*.py",
    "witty_agent_server/application/services/agent/openclaw_*.py",
    "witty_agent_server/application/services/session/dsh_*.py",
    "witty_agent_server/application/services/session/openclaw_*.py",
    "witty_agent_server/application/services/skill/dsh_*.py",
    "witty_agent_server/application/services/skill/openclaw_*.py",
    "witty_agent_server/infra/clients/dsh_plugins/*",
    "witty_agent_server/infra/clients/dsh_client.py",
    "witty_agent_server/infra/clients/openclaw_gateway_client.py",
    "witty_agent_server/runtimes/dsh_runtime.py",
    "witty_agent_server/runtimes/openclaw_*.py",
    "witty_service/sandbox/e2b.py",
)

# 已从 pyproject.toml 删除的运行时依赖，规范化后比较（小写、-/_/. 等价）。
FORBIDDEN_DISTRIBUTIONS = (
    "deepseek-harness",
    "deepseek-harness-sdk",
    "e2b",
    "openclaw",
)

# 上述 e2b 依赖的实际发行名包含后缀，例如 e2b-code-interpreter。
FORBIDDEN_DISTRIBUTION_PREFIXES = ("e2b-",)


def _normalize_distribution(name: str) -> str:
    """按 PEP 503 规范化分发包名，``-``/``_``/``.`` 等价且连续分隔符折叠。"""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _iter_module_names(names: Iterable[str]) -> Iterable[str]:
    for name in names:
        if not name.endswith(".py"):
            continue
        # 按顶层路径段判断，避免 ``witty_service-0.1.0.data/`` 等
        # 同前缀目录被误认为受管包。
        if name.split("/", 1)[0] in PACKAGE_ROOTS:
            yield name


def check_modules_present_in_source(
    module_names: Iterable[str], src_root: Path
) -> list[str]:
    """wheel 中不得存在 src/ 已删除的 Python 模块（抓 build/lib 残留）。"""
    problems: list[str] = []
    for name in module_names:
        if not (src_root / name).is_file():
            problems.append(
                f"wheel 中存在 src/ 已不存在的模块: {name} "
                f"(检查 build/lib 是否残留旧文件)"
            )
    return problems


def check_forbidden_modules(module_names: Iterable[str]) -> list[str]:
    """wheel 中不得存在已删除 Runtime/Sandbox 的模块。"""
    problems: list[str] = []
    for name in module_names:
        if name.startswith(FORBIDDEN_PREFIXES) or any(
            fnmatch.fnmatch(name, pattern) for pattern in FORBIDDEN_GLOBS
        ):
            problems.append(f"wheel 中包含已删除的模块: {name}")
    return problems


def parse_requires_dist(metadata_text: str) -> list[str]:
    """从 METADATA 文本中提取 Requires-Dist 的分发名。"""
    requirements: list[str] = []
    for line in metadata_text.splitlines():
        if not line.lower().startswith("requires-dist:"):
            continue
        spec = line.split(":", 1)[1].strip()
        name = spec.split(";", 1)[0].strip()
        for separator in ("[", " ", "<", ">", "=", "!", "~", "(", ")"):
            name = name.split(separator, 1)[0]
        if name:
            requirements.append(name.strip())
    return requirements


def check_forbidden_distributions(requirements: Iterable[str]) -> list[str]:
    """METADATA 不得重新引入已删除依赖。"""
    problems: list[str] = []
    for distribution in requirements:
        normalized = _normalize_distribution(distribution)
        if normalized in FORBIDDEN_DISTRIBUTIONS or normalized.startswith(
            FORBIDDEN_DISTRIBUTION_PREFIXES
        ):
            problems.append(f"wheel METADATA 中包含已删除依赖: {distribution}")
    return problems


def check_wheel(wheel_path: Path, src_root: Path) -> list[str]:
    """返回 wheel 的全部问题；空列表代表通过。"""
    problems: list[str] = []
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()

    module_names = list(_iter_module_names(names))
    problems.extend(check_modules_present_in_source(module_names, src_root))
    problems.extend(check_forbidden_modules(module_names))

    metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
    if not metadata_names:
        problems.append(f"wheel 中缺少 METADATA: {wheel_path}")
    for metadata_name in metadata_names:
        with zipfile.ZipFile(wheel_path) as archive:
            metadata_text = archive.read(metadata_name).decode("utf-8", "replace")
        problems.extend(
            check_forbidden_distributions(parse_requires_dist(metadata_text))
        )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", nargs="+", type=Path, help="待检查的 .whl 文件")
    parser.add_argument(
        "--src-root",
        type=Path,
        default=Path("src"),
        help="源码根目录，默认 src",
    )
    args = parser.parse_args(argv)

    failed = False
    for wheel in args.wheels:
        problems = check_wheel(wheel, args.src_root)
        if problems:
            failed = True
            print(f"[FAIL] {wheel}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"[OK] {wheel}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
