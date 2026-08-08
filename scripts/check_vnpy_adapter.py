# -*- coding: utf-8 -*-
"""Smoke-check the optional vn.py adapter without requiring vn.py."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sys
import tempfile
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

SMOKE_MATCHING_MODE = "fixed_delay_limit"
RECONNECT_PENDING_DELAY_MS = 500


def _smoke_gateway_settings(**overrides: Any) -> Dict[str, Any]:
    """Return deterministic settings isolated from deployed DSA_SIM env values."""

    settings: Dict[str, Any] = {
        "fill_delay_ms": 50,
        "matching_mode": SMOKE_MATCHING_MODE,
        "preserve_state_on_reconnect": True,
        "reject_every_nth_order": 0,
        "duplicate_trade_event_count": 1,
    }
    settings.update(overrides)
    return settings


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


def _wait_for_terminal_order(main_engine: Any, vt_orderid: str, *, timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    order = None
    while time.monotonic() < deadline:
        order = main_engine.get_order(vt_orderid)
        status_name = getattr(getattr(order, "status", None), "name", "")
        if status_name in {"ALLTRADED", "REJECTED", "CANCELLED"}:
            break
        time.sleep(0.05)
    return order


def _run_reconnect_soak(
    *,
    main_engine: Any,
    payload: Dict[str, Any],
    cycles: int,
) -> Dict[str, Any]:
    gateway = main_engine.get_gateway("DSA_SIM")
    if gateway is None or not callable(getattr(gateway, "get_state_snapshot", None)):
        raise VnpyAdapterError("built-in simulated gateway state diagnostics are unavailable")

    records = []
    order_ids: set[str] = set()
    trade_ids: set[str] = set()
    bridge = VnpyMainEngineBridge(main_engine=main_engine, gateway_name="DSA_SIM")
    for index in range(1, cycles + 1):
        main_engine.connect(
            _smoke_gateway_settings(fill_delay_ms=RECONNECT_PENDING_DELAY_MS),
            "DSA_SIM",
        )
        cycle_payload = {
            **payload,
            "source": "adapter_reconnect_soak",
            "plan_uid": f"reconnect-soak-{index}",
            "reference": f"dsa:adapter_reconnect_soak:reconnect-soak-{index}",
        }
        submission = bridge.send_order(cycle_payload)
        vt_orderid = str(submission.get("vt_orderid") or "")
        if not submission.get("accepted") or not vt_orderid or vt_orderid in order_ids:
            raise VnpyAdapterError(f"reconnect cycle {index} did not create a unique order")
        order_ids.add(vt_orderid)
        time.sleep(0.05)

        gateway.close()
        disconnected_state = gateway.get_state_snapshot()
        pending_retained = vt_orderid.rsplit(".", 1)[-1] in set(
            disconnected_state.get("active_order_ids") or []
        )
        if not pending_retained:
            raise VnpyAdapterError(
                f"reconnect cycle {index} lost its in-flight order while disconnected"
            )
        main_engine.connect(_smoke_gateway_settings(), "DSA_SIM")
        order = _wait_for_terminal_order(main_engine, vt_orderid)
        status_name = getattr(getattr(order, "status", None), "name", None)
        trades = [
            trade
            for trade in main_engine.get_all_trades()
            if str(getattr(trade, "vt_orderid", "")) == vt_orderid
        ]
        if status_name != "ALLTRADED" or len(trades) != 1:
            raise VnpyAdapterError(
                f"reconnect cycle {index} expected one fill, got status={status_name} trades={len(trades)}"
            )
        vt_tradeid = str(getattr(trades[0], "vt_tradeid", ""))
        if not vt_tradeid or vt_tradeid in trade_ids:
            raise VnpyAdapterError(f"reconnect cycle {index} produced a duplicate trade id")
        trade_ids.add(vt_tradeid)
        records.append(
            {
                "cycle": index,
                "vt_orderid": vt_orderid,
                "vt_tradeid": vt_tradeid,
                "pending_retained": pending_retained,
                "order_status": status_name,
                "trade_count": len(trades),
                "state_after": gateway.get_state_snapshot(),
            }
        )

    final_state = gateway.get_state_snapshot()
    return {
        "cycles": cycles,
        "completed_cycles": len(records),
        "all_filled_once": len(records) == cycles,
        "all_pending_retained": all(item["pending_retained"] for item in records),
        "unique_order_count": len(order_ids),
        "unique_trade_count": len(trade_ids),
        "final_state": final_state,
        "records": records,
    }


def _run_fault_matrix(
    *,
    main_engine: Any,
    event_engine: Any,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    from vnpy.trader.event import EVENT_TRADE

    gateway = main_engine.get_gateway("DSA_SIM")
    if gateway is None or not callable(getattr(gateway, "get_state_snapshot", None)):
        raise VnpyAdapterError("built-in simulated gateway fault diagnostics are unavailable")

    before = gateway.get_state_snapshot()
    reject_order_number = int(before.get("order_count") or 0) + 2
    fault_settings = _smoke_gateway_settings(
        reject_every_nth_order=reject_order_number,
        duplicate_trade_event_count=2,
    )
    observed_trade_ids: list[str] = []

    def _capture_trade(event: Any) -> None:
        observed_trade_ids.append(str(getattr(getattr(event, "data", None), "vt_tradeid", "")))

    event_engine.register(EVENT_TRADE, _capture_trade)
    try:
        main_engine.connect(fault_settings, "DSA_SIM")
        bridge = VnpyMainEngineBridge(main_engine=main_engine, gateway_name="DSA_SIM")
        filled_submission = bridge.send_order(
            {
                **payload,
                "source": "adapter_fault_matrix",
                "plan_uid": "fault-matrix-fill",
                "reference": "dsa:adapter_fault_matrix:fault-matrix-fill",
            }
        )
        filled_orderid = str(filled_submission.get("vt_orderid") or "")
        filled_order = _wait_for_terminal_order(main_engine, filled_orderid)

        rejected_submission = bridge.send_order(
            {
                **payload,
                "source": "adapter_fault_matrix",
                "plan_uid": "fault-matrix-reject",
                "reference": "dsa:adapter_fault_matrix:fault-matrix-reject",
            }
        )
        rejected_orderid = str(rejected_submission.get("vt_orderid") or "")
        rejected_order = _wait_for_terminal_order(main_engine, rejected_orderid)

        callback_deadline = time.monotonic() + 3.0
        filled_trade_ids: list[str] = []
        stored_trades: list[Any] = []
        while time.monotonic() < callback_deadline:
            stored_trades = [
                trade
                for trade in main_engine.get_all_trades()
                if str(getattr(trade, "vt_orderid", "")) == filled_orderid
            ]
            target_trade_ids = {
                str(getattr(trade, "vt_tradeid", ""))
                for trade in stored_trades
                if str(getattr(trade, "vt_tradeid", ""))
            }
            filled_trade_ids = [
                trade_id
                for trade_id in observed_trade_ids
                if trade_id in target_trade_ids
            ]
            if len(stored_trades) == 1 and len(filled_trade_ids) >= 2:
                break
            time.sleep(0.05)
        filled_status = getattr(getattr(filled_order, "status", None), "name", None)
        rejected_status = getattr(getattr(rejected_order, "status", None), "name", None)
        rejected_reason = str(getattr(rejected_order, "rejected_reason", "") or "")
        unique_callback_ids = set(filled_trade_ids)
        if filled_status != "ALLTRADED":
            raise VnpyAdapterError(f"fault matrix fill order ended as {filled_status}")
        if rejected_status != "REJECTED" or rejected_reason != "simulated_configured_rejection":
            raise VnpyAdapterError(
                "fault matrix rejection was not surfaced as simulated_configured_rejection"
            )
        if len(filled_trade_ids) != 2 or len(unique_callback_ids) != 1:
            raise VnpyAdapterError(
                "fault matrix expected two callbacks carrying one duplicate trade id"
            )
        if len(stored_trades) != 1:
            raise VnpyAdapterError(
                f"fault matrix expected one deduplicated MainEngine trade, got {len(stored_trades)}"
            )
        return {
            "ok": True,
            "filled_order_status": filled_status,
            "rejected_order_status": rejected_status,
            "rejected_reason": rejected_reason,
            "duplicate_trade_callback_count": len(filled_trade_ids),
            "unique_trade_callback_count": len(unique_callback_ids),
            "main_engine_trade_count": len(stored_trades),
            "state_after": gateway.get_state_snapshot(),
        }
    finally:
        event_engine.unregister(EVENT_TRADE, _capture_trade)
        main_engine.connect(
            {
                "preserve_state_on_reconnect": True,
                "reject_every_nth_order": 0,
                "duplicate_trade_event_count": 1,
            },
            "DSA_SIM",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-vnpy",
        action="store_true",
        help="Return a non-zero exit code when the optional vn.py runtime is unavailable.",
    )
    parser.add_argument(
        "--reconnect-cycles",
        type=int,
        default=0,
        help="Run N disconnect/reconnect cycles with an in-flight DSA_SIM order (0-50).",
    )
    parser.add_argument(
        "--fault-matrix",
        action="store_true",
        help=(
            "Verify one configured rejection and duplicate trade-event deduplication "
            "through the real DSA_SIM MainEngine/EventEngine path."
        ),
    )
    args = parser.parse_args(argv)
    if args.reconnect_cycles < 0 or args.reconnect_cycles > 50:
        parser.error("--reconnect-cycles must be between 0 and 50")
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
        runtime_workspace = None
        previous_working_directory = None
        vnpy_settings = None
        previous_vnpy_file_logging = None
        vnpy_file_logging_was_configured = False
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
            runtime_workspace = tempfile.TemporaryDirectory(
                prefix="dsa-vnpy-adapter-smoke-",
                ignore_cleanup_errors=True,
            )
            previous_working_directory = Path.cwd()
            os.chdir(runtime_workspace.name)
            from vnpy.trader.setting import SETTINGS as runtime_vnpy_settings

            vnpy_settings = runtime_vnpy_settings
            vnpy_file_logging_was_configured = "log.file" in vnpy_settings
            previous_vnpy_file_logging = vnpy_settings.get("log.file")
            vnpy_settings["log.file"] = False
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
                "runtime_data_dir_isolated": True,
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
            runtime_handle.main_engine.connect(
                _smoke_gateway_settings(),
                "DSA_SIM",
            )
            simulated_submission = VnpyMainEngineBridge(
                main_engine=runtime_handle.main_engine,
                gateway_name="DSA_SIM",
            ).send_order(payload)
            simulated_orderid = str(simulated_submission.get("vt_orderid") or "")
            simulated_order = _wait_for_terminal_order(
                runtime_handle.main_engine,
                simulated_orderid,
            )
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
                "matching_mode": SMOKE_MATCHING_MODE,
                "isolated_from_deployed_matching_mode": True,
            }
            if not result["simulated_gateway_smoke"]["filled"]:
                raise VnpyAdapterError("built-in vn.py simulated gateway did not fill the smoke order")
            if args.fault_matrix:
                result["fault_matrix"] = _run_fault_matrix(
                    main_engine=runtime_handle.main_engine,
                    event_engine=runtime_handle.event_engine,
                    payload=payload,
                )
            if args.reconnect_cycles:
                result["reconnect_soak"] = _run_reconnect_soak(
                    main_engine=runtime_handle.main_engine,
                    payload=payload,
                    cycles=args.reconnect_cycles,
                )
        except Exception as exc:  # noqa: BLE001 - script should print actionable diagnostics.
            result["ok"] = False
            result["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        finally:
            if runtime_handle is not None:
                runtime_handle.close()
            if vnpy_settings is not None:
                if vnpy_file_logging_was_configured:
                    vnpy_settings["log.file"] = previous_vnpy_file_logging
                else:
                    vnpy_settings.pop("log.file", None)
            if previous_working_directory is not None:
                os.chdir(previous_working_directory)
            if runtime_workspace is not None:
                runtime_workspace.cleanup()
    else:
        result["fallback"] = "vnpy is not importable; DSA local paper mode remains usable."
        if args.require_vnpy or args.reconnect_cycles or args.fault_matrix:
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
