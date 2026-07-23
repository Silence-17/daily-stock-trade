# -*- coding: utf-8 -*-
"""Install and verify explicitly declared vn.py gateway plugins."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:  # pragma: no cover - available with pip in fresh venvs.
    from pip._vendor.packaging.requirements import (  # type: ignore[no-redef]
        InvalidRequirement,
        Requirement,
    )


ENV_NAME = "VNPY_GATEWAY_PLUGINS_JSON"
MODULE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)


@dataclass(frozen=True)
class GatewayPlugin:
    package: str
    module: str


def parse_gateway_plugins(raw: str | None) -> list[GatewayPlugin]:
    text = (raw or "").strip() or "[]"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("gateway plugin manifest must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("gateway plugin manifest must be a JSON array")

    plugins: list[GatewayPlugin] = []
    seen_packages: set[str] = set()
    seen_modules: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"gateway plugin item {index} must be an object")
        unknown = sorted(set(item) - {"package", "module"})
        if unknown:
            raise ValueError(
                f"gateway plugin item {index} has unsupported keys: {', '.join(unknown)}"
            )
        package = item.get("package")
        module = item.get("module")
        if not isinstance(package, str) or not package.strip():
            raise ValueError(f"gateway plugin item {index} requires package")
        if not isinstance(module, str) or not MODULE_PATTERN.fullmatch(module.strip()):
            raise ValueError(f"gateway plugin item {index} has an invalid module")
        package = package.strip()
        module = module.strip()
        try:
            requirement = Requirement(package)
        except InvalidRequirement as exc:
            raise ValueError(
                f"gateway plugin item {index} has an invalid package requirement"
            ) from exc
        if requirement.url is not None or requirement.marker is not None:
            raise ValueError(
                "gateway plugin packages must use registry requirements without URLs or markers"
            )
        specifiers = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator not in {"==", "==="}
            or "*" in specifiers[0].version
        ):
            raise ValueError(
                "gateway plugin packages must pin one exact version with == or ==="
            )
        package_key = requirement.name.lower().replace("_", "-")
        module_key = module.lower()
        if package_key in seen_packages:
            raise ValueError(f"duplicate gateway plugin package: {requirement.name}")
        if module_key in seen_modules:
            raise ValueError(f"duplicate gateway plugin module: {module}")
        seen_packages.add(package_key)
        seen_modules.add(module_key)
        plugins.append(GatewayPlugin(package=package, module=module))
    return plugins


def install_gateway_plugins(plugins: Sequence[GatewayPlugin]) -> None:
    if not plugins:
        return
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--prefer-binary",
        "--extra-index-url",
        "https://pypi.vnpy.com",
        *(plugin.package for plugin in plugins),
    ]
    subprocess.run(command, check=True)


def verify_gateway_plugins(plugins: Iterable[GatewayPlugin]) -> None:
    for plugin in plugins:
        importlib.import_module(plugin.module)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-json",
        default=os.getenv(ENV_NAME, "[]"),
        help=f"JSON array; defaults to {ENV_NAME} or an empty array.",
    )
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--assert-empty", action="store_true")
    parser.add_argument("--print-modules-json", action="store_true")
    parser.add_argument("--print-modules-lines", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plugins = parse_gateway_plugins(args.manifest_json)
        if args.assert_empty and plugins:
            raise ValueError("gateway plugins require the optional vn.py runtime")
        if args.install:
            install_gateway_plugins(plugins)
        if args.verify:
            verify_gateway_plugins(plugins)
    except (ImportError, subprocess.CalledProcessError, ValueError) as exc:
        parser.exit(2, f"ERROR: {exc}\n")

    modules = [plugin.module for plugin in plugins]
    if args.print_modules_json:
        print(json.dumps(modules, separators=(",", ":")))
    if args.print_modules_lines:
        for module in modules:
            print(module)
    if not args.print_modules_json and not args.print_modules_lines:
        print(json.dumps({"ok": True, "plugin_count": len(plugins)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
