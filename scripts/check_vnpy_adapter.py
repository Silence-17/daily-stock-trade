# -*- coding: utf-8 -*-
"""Smoke-check the optional vn.py adapter without requiring vn.py."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.vnpy_adapter import (  # noqa: E402
    VnpyAdapterError,
    VnpyMainEngineBridge,
    build_vnpy_order_request_payload,
    create_vnpy_order_request,
    get_vnpy_adapter_status,
)
from src.services.vnpy_runtime import (  # noqa: E402
    VnpyRuntimeSettings,
    bootstrap_vnpy_runtime,
)


class _SmokeMainEngine:
    def __init__(self) -> None:
        self.request = None
        self.gateway_name = None

    def send_order(self, request: Any, gateway_name: str) -> str:
        self.request = request
        self.gateway_name = gateway_name
        return f"{gateway_name}.SMOKE"


def _installed_vnpy_version() -> str | None:
    try:
        return importlib.metadata.version("vnpy")
    except importlib.metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-vnpy",
        action="store_true",
        help="Return a non-zero exit code when the optional vn.py runtime is unavailable.",
    )
    args = parser.parse_args(argv)
    status = get_vnpy_adapter_status()
    payload = build_vnpy_order_request_payload(
        symbol="600519",
        side="buy",
        market="cn",
        cash_amount=1200,
        price=10,
        source="adapter_smoke",
        plan_uid="smoke",
    )
    result: Dict[str, Any] = {
        "ok": True,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "vnpy_version": _installed_vnpy_version(),
            "published_classifier_range": "3.10-3.13",
            "python_classifier_verified": (3, 10) <= sys.version_info[:2] <= (3, 13),
        },
        "vnpy_adapter": status,
        "sample_order_request_payload": payload,
    }
    if status.get("available"):
        runtime_handle = None
        try:
            order_request = create_vnpy_order_request(payload)
            result["order_request_class"] = (
                f"{order_request.__class__.__module__}.{order_request.__class__.__name__}"
            )
            engine = _SmokeMainEngine()
            submission = VnpyMainEngineBridge(
                main_engine=engine,
                gateway_name="SIM",
            ).send_order(payload)
            result["bridge_smoke"] = {
                "accepted": submission.get("accepted"),
                "vt_orderid": submission.get("vt_orderid"),
                "gateway_name": submission.get("gateway_name"),
                "request_class": (
                    f"{engine.request.__class__.__module__}.{engine.request.__class__.__name__}"
                    if engine.request is not None
                    else None
                ),
            }
            runtime_handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class=(
                        "src.services.vnpy_simulated_gateway:DsaSimulatedGateway"
                    ),
                    gateway_name="DSA_SIM",
                    connect_on_start=True,
                    auto_attach_events=False,
                )
            )
            result["runtime_smoke"] = {
                "available": runtime_handle.diagnostics.get("available"),
                "mode": runtime_handle.diagnostics.get("mode"),
                "reason": runtime_handle.diagnostics.get("reason"),
                "runtime_data_dir": runtime_handle.diagnostics.get("runtime_data_dir"),
                "event_engine_class": (
                    f"{runtime_handle.event_engine.__class__.__module__}."
                    f"{runtime_handle.event_engine.__class__.__name__}"
                    if runtime_handle.event_engine is not None
                    else None
                ),
                "main_engine_class": (
                    f"{runtime_handle.main_engine.__class__.__module__}."
                    f"{runtime_handle.main_engine.__class__.__name__}"
                    if runtime_handle.main_engine is not None
                    else None
                ),
            }
            if not runtime_handle.diagnostics.get("available"):
                raise VnpyAdapterError(
                    str(runtime_handle.diagnostics.get("message") or "vn.py runtime bootstrap failed")
                )
            simulated_submission = VnpyMainEngineBridge(
                main_engine=runtime_handle.main_engine,
                gateway_name="DSA_SIM",
            ).send_order(payload)
            simulated_orderid = str(simulated_submission.get("vt_orderid") or "")
            deadline = time.monotonic() + 3.0
            simulated_order = None
            while time.monotonic() < deadline:
                simulated_order = runtime_handle.main_engine.get_order(simulated_orderid)
                status_name = getattr(getattr(simulated_order, "status", None), "name", "")
                if status_name in {"ALLTRADED", "REJECTED", "CANCELLED"}:
                    break
                time.sleep(0.05)
            simulated_trades = [
                trade
                for trade in runtime_handle.main_engine.get_all_trades()
                if str(getattr(trade, "vt_orderid", "")) == simulated_orderid
            ]
            simulated_status = getattr(getattr(simulated_order, "status", None), "name", None)
            result["simulated_gateway_smoke"] = {
                "accepted": simulated_submission.get("accepted"),
                "connected": runtime_handle.diagnostics.get("connect", {}).get("connected"),
                "gateway_name": "DSA_SIM",
                "vt_orderid": simulated_orderid or None,
                "order_status": simulated_status,
                "trade_count": len(simulated_trades),
                "filled": simulated_status == "ALLTRADED" and bool(simulated_trades),
            }
            if not result["simulated_gateway_smoke"]["filled"]:
                raise VnpyAdapterError("built-in vn.py simulated gateway did not fill the smoke order")
        except Exception as exc:  # noqa: BLE001 - script should print actionable diagnostics.
            result["ok"] = False
            result["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        finally:
            if runtime_handle is not None:
                runtime_handle.close()
    else:
        result["fallback"] = "vnpy is not importable; DSA local paper mode remains usable."
        if args.require_vnpy:
            result["ok"] = False
            result["error"] = {
                "type": "VnpyRuntimeUnavailable",
                "message": str(status.get("reason") or "vn.py is not importable"),
            }

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VnpyAdapterError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(1)
