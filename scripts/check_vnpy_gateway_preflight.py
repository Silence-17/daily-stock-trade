# -*- coding: utf-8 -*-
"""Validate a vn.py gateway and connection-file contract without connecting."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.vnpy_runtime import (  # noqa: E402
    VnpyRuntimeSettings,
    bootstrap_vnpy_runtime,
    load_vnpy_runtime_settings,
)

BUILTIN_GATEWAY_CLASS = "src.services.vnpy_simulated_gateway:DsaSimulatedGateway"
BUILTIN_GATEWAY_NAME = "DSA_SIM"


def evaluate_preflight(
    *,
    runtime_available: bool,
    gateway_registered: bool,
    gateway_class: str,
    gateway_name: str,
    settings_required: bool,
    settings_provided: bool,
    settings_valid: bool,
    settings_inside_repository: bool,
    missing_default_keys: Iterable[str],
    require_external_gateway: bool,
    require_settings_outside_repository: bool,
    require_all_default_keys: bool,
) -> Dict[str, Any]:
    failures = []
    if not gateway_class:
        failures.append("gateway_class_required")
    if not runtime_available:
        failures.append("runtime_unavailable")
    if not gateway_registered:
        failures.append("gateway_registration_failed")
    if not gateway_name:
        failures.append("gateway_name_unresolved")
    if require_external_gateway and _is_builtin_gateway(gateway_class, gateway_name):
        failures.append("builtin_gateway_not_external")
    if settings_required and not settings_provided:
        failures.append("connect_settings_required")
    if settings_provided and not settings_valid:
        failures.append("connect_settings_invalid")
    if (
        require_settings_outside_repository
        and settings_provided
        and settings_inside_repository
    ):
        failures.append("connect_settings_inside_repository")
    missing = sorted({str(item) for item in missing_default_keys if str(item)})
    if require_all_default_keys and missing:
        failures.append("default_setting_keys_missing")
    return {
        "ok": not failures,
        "failures": failures,
        "requirements": {
            "external_gateway": bool(require_external_gateway),
            "settings_outside_repository": bool(
                require_settings_outside_repository
            ),
            "all_default_setting_keys": bool(require_all_default_keys),
        },
    }


def inspect_preflight(
    *,
    gateway_class: str,
    gateway_name: Optional[str],
    settings_path: Optional[Path],
    require_external_gateway: bool = False,
    require_settings_outside_repository: bool = False,
    require_all_default_keys: bool = False,
) -> Dict[str, Any]:
    clean_class = str(gateway_class or "").strip()
    clean_name = str(gateway_name or "").strip()
    payload, settings_diagnostics = _read_settings_keys(settings_path)
    handle = None
    runtime_diagnostics: Dict[str, Any] = {}
    gateway = None
    with tempfile.TemporaryDirectory(prefix="dsa-vnpy-preflight-") as tmp:
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp)
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class=clean_class or None,
                    gateway_name=clean_name or None,
                    connect_settings_path=None,
                    connect_on_start=False,
                    auto_attach_events=False,
                    auto_reconnect_enabled=False,
                )
            )
            runtime_diagnostics = dict(handle.diagnostics or {})
            gateway = _registered_gateway(handle.main_engine, clean_name)
            if gateway is not None:
                clean_name = str(
                    getattr(gateway, "gateway_name", None)
                    or clean_name
                    or getattr(type(gateway), "default_name", None)
                    or ""
                ).strip()
        finally:
            if handle is not None:
                handle.close()
            _close_log_handlers_under(Path(tmp))
            os.chdir(original_cwd)

    default_setting = getattr(gateway, "default_setting", {}) if gateway is not None else {}
    if not isinstance(default_setting, dict):
        default_setting = {}
    expected_keys = sorted(str(key) for key in default_setting)
    provided_keys = sorted(str(key) for key in payload) if isinstance(payload, dict) else []
    missing_keys = sorted(set(expected_keys) - set(provided_keys))
    unknown_keys = sorted(set(provided_keys) - set(expected_keys))
    empty_keys = sorted(
        str(key)
        for key, value in (payload or {}).items()
        if value is None or (isinstance(value, str) and not value.strip())
    )
    connect_without_settings = bool(
        getattr(gateway, "connect_without_settings", False)
    ) if gateway is not None else False
    gateway_diagnostics = runtime_diagnostics.get("gateway")
    if not isinstance(gateway_diagnostics, dict):
        gateway_diagnostics = {}
    runtime_available = bool(runtime_diagnostics.get("available"))
    gateway_registered = bool(gateway_diagnostics.get("added")) and gateway is not None
    outside_required = bool(
        require_settings_outside_repository or require_external_gateway
    )
    evaluation = evaluate_preflight(
        runtime_available=runtime_available,
        gateway_registered=gateway_registered,
        gateway_class=clean_class,
        gateway_name=clean_name,
        settings_required=not connect_without_settings,
        settings_provided=bool(settings_diagnostics["provided"]),
        settings_valid=bool(settings_diagnostics["valid"]),
        settings_inside_repository=bool(settings_diagnostics["inside_repository"]),
        missing_default_keys=missing_keys,
        require_external_gateway=require_external_gateway,
        require_settings_outside_repository=outside_required,
        require_all_default_keys=require_all_default_keys,
    )
    warnings = []
    if missing_keys and not require_all_default_keys:
        warnings.append("default_setting_keys_missing")
    if unknown_keys:
        warnings.append("unknown_setting_keys")
    if empty_keys:
        warnings.append("empty_setting_values")
    return {
        "schema_version": 1,
        "ok": evaluation["ok"],
        "gateway": {
            "class": clean_class or None,
            "name": clean_name or None,
            "registered": gateway_registered,
            "external": not _is_builtin_gateway(clean_class, clean_name),
            "connect_without_settings": connect_without_settings,
            "default_setting_key_count": len(expected_keys),
            "default_setting_keys": expected_keys,
        },
        "settings": {
            **settings_diagnostics,
            "provided_key_count": len(provided_keys),
            "provided_keys": provided_keys,
            "missing_default_keys": missing_keys,
            "unknown_keys": unknown_keys,
            "empty_value_keys": empty_keys,
        },
        "runtime": {
            "available": runtime_available,
            "mode": runtime_diagnostics.get("mode"),
            "gateway_registration_reason": gateway_diagnostics.get("reason"),
            "error_type": (
                gateway_diagnostics.get("error_type")
                or runtime_diagnostics.get("error_type")
            ),
        },
        "safety": {
            "connect_called": False,
            "network_calls": False,
            "orders_created": False,
            "subscriptions_created": False,
            "settings_values_exposed": False,
            "settings_path_exposed": False,
        },
        "warnings": warnings,
        "evaluation": evaluation,
    }


def _read_settings_keys(path: Optional[Path]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if path is None:
        return {}, {
            "provided": False,
            "valid": False,
            "inside_repository": False,
            "read_error_type": None,
        }
    expanded = path.expanduser()
    inside_repository = _is_within_repository(expanded)
    try:
        payload = json.loads(expanded.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - output only the type, never path or content.
        return {}, {
            "provided": True,
            "valid": False,
            "inside_repository": inside_repository,
            "read_error_type": type(exc).__name__,
        }
    if not isinstance(payload, dict):
        return {}, {
            "provided": True,
            "valid": False,
            "inside_repository": inside_repository,
            "read_error_type": "SettingsMustBeObject",
        }
    return payload, {
        "provided": True,
        "valid": True,
        "inside_repository": inside_repository,
        "read_error_type": None,
    }


def _registered_gateway(main_engine: Any, gateway_name: str) -> Any:
    if main_engine is None:
        return None
    get_gateway = getattr(main_engine, "get_gateway", None)
    if gateway_name and callable(get_gateway):
        gateway = get_gateway(gateway_name)
        if gateway is not None:
            return gateway
    get_all = getattr(main_engine, "get_all_gateways", None)
    gateways = list(get_all() or []) if callable(get_all) else []
    return gateways[0] if len(gateways) == 1 else None


def _is_builtin_gateway(gateway_class: str, gateway_name: str) -> bool:
    clean_class = str(gateway_class or "").strip()
    clean_name = str(gateway_name or "").strip().upper()
    return (
        clean_class == BUILTIN_GATEWAY_CLASS
        or clean_class.endswith(":DsaSimulatedGateway")
        or clean_class.endswith(".DsaSimulatedGateway")
        or clean_name == BUILTIN_GATEWAY_NAME
    )


def _is_within_repository(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def _close_log_handlers_under(root: Path) -> None:
    """Release vn.py preflight log files without touching application handlers."""

    resolved_root = root.resolve()
    loggers = [logging.getLogger()]
    loggers.extend(
        item
        for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    )
    for logger in loggers:
        for handler in list(logger.handlers):
            filename = getattr(handler, "baseFilename", None)
            if not filename:
                continue
            try:
                Path(filename).resolve(strict=False).relative_to(resolved_root)
            except ValueError:
                continue
            handler.close()
            logger.removeHandler(handler)

    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return

    handlers = list(getattr(loguru_logger._core, "handlers", {}).items())
    for handler_id, handler in handlers:
        sink = getattr(handler, "_sink", None)
        filename = getattr(sink, "_path", None)
        if not filename:
            continue
        try:
            Path(filename).resolve(strict=False).relative_to(resolved_root)
        except ValueError:
            continue
        loguru_logger.remove(handler_id)


def main(argv: Optional[list[str]] = None) -> int:
    env = load_vnpy_runtime_settings()
    parser = argparse.ArgumentParser(
        description="Preflight a vn.py gateway without connecting or placing orders."
    )
    parser.add_argument("--gateway-class", default=env.gateway_class)
    parser.add_argument("--gateway-name", default=env.gateway_name)
    parser.add_argument(
        "--settings-path",
        type=Path,
        default=Path(env.connect_settings_path) if env.connect_settings_path else None,
    )
    parser.add_argument("--require-external-gateway", action="store_true")
    parser.add_argument("--require-settings-outside-repository", action="store_true")
    parser.add_argument("--require-all-default-keys", action="store_true")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)

    result = inspect_preflight(
        gateway_class=str(args.gateway_class or ""),
        gateway_name=args.gateway_name,
        settings_path=args.settings_path,
        require_external_gateway=bool(args.require_external_gateway),
        require_settings_outside_repository=bool(
            args.require_settings_outside_repository
        ),
        require_all_default_keys=bool(args.require_all_default_keys),
    )
    output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json is not None:
        args.output_json.expanduser().write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
