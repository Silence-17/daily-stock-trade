#!/usr/bin/env python3
"""Run a bounded XTP paper-order acceptance check through the deployed API."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


CONFIRMATION = "XTP_PAPER_ORDER"
MAX_QUANTITY = 100
MAX_NOTIONAL = 2_000.0
ACTIVE_STATUSES = {
    "submitting",
    "nottraded",
    "not_traded",
    "parttraded",
    "part_traded",
    "part_filled",
    "partial_filled",
    "cancel_requested",
}
TERMINAL_STATUSES = {"alltraded", "filled", "cancelled", "canceled", "rejected", "failed"}


class AcceptanceError(RuntimeError):
    """Raised when a safety or acceptance condition is not satisfied."""


@dataclass
class ApiClient:
    base_url: str
    timeout_seconds: float = 15.0

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - operator-supplied URL.
                return json.loads(response.read().decode("utf-8-sig"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AcceptanceError(f"API {method} {path} returned HTTP {exc.code}: {detail[:300]}") from exc
        except (URLError, TimeoutError) as exc:
            raise AcceptanceError(f"API {method} {path} failed: {exc}") from exc

    def get_status(self) -> Dict[str, Any]:
        return self.request(
            "GET",
            "/api/v1/vnpy-paper/status?include_snapshot=true&include_recent_trades=true",
        )


def _runtime(status: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = status.get("diagnostics") if isinstance(status.get("diagnostics"), dict) else {}
    runtime = diagnostics.get("vnpy_runtime") if isinstance(diagnostics.get("vnpy_runtime"), dict) else {}
    return runtime


def _sync_state(status: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = status.get("diagnostics") if isinstance(status.get("diagnostics"), dict) else {}
    state = diagnostics.get("vnpy_sync_state") if isinstance(diagnostics.get("vnpy_sync_state"), dict) else {}
    return state


def _gateway_name(status: Dict[str, Any]) -> str:
    runtime = _runtime(status)
    gateway = runtime.get("gateway") if isinstance(runtime.get("gateway"), dict) else {}
    settings = status.get("settings") if isinstance(status.get("settings"), dict) else {}
    return str(gateway.get("gateway_name") or settings.get("vnpy_gateway_name") or "").strip()


def _connection_confirmed(status: Dict[str, Any]) -> bool:
    runtime = _runtime(status)
    connect = runtime.get("connect") if isinstance(runtime.get("connect"), dict) else {}
    return connect.get("connected") is True


def _external_preflight_ok(status: Dict[str, Any]) -> bool:
    runtime = _runtime(status)
    preflight = runtime.get("production_preflight")
    return (
        isinstance(preflight, dict)
        and preflight.get("ok") is True
        and preflight.get("external_gateway") is True
    )


def _active_xtp_orders(status: Dict[str, Any]) -> list[Dict[str, Any]]:
    orders = _sync_state(status).get("recent_orders")
    if not isinstance(orders, list):
        return []
    return [
        item
        for item in orders
        if isinstance(item, dict)
        and str(item.get("vt_orderid") or "").upper().startswith("XTP.")
        and str(item.get("status") or "").strip().lower() in ACTIVE_STATUSES
    ]


def _find_order(status: Dict[str, Any], vt_orderid: str) -> Optional[Dict[str, Any]]:
    orders = _sync_state(status).get("recent_orders")
    if not isinstance(orders, list):
        return None
    return next(
        (
            item
            for item in orders
            if isinstance(item, dict) and str(item.get("vt_orderid") or "") == vt_orderid
        ),
        None,
    )


def _local_ledger_summary(status: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}
    accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), list) else []
    positions = accounts[0].get("positions") if accounts and isinstance(accounts[0], dict) else []
    return {
        "account_id": (status.get("account") or {}).get("id") if isinstance(status.get("account"), dict) else None,
        "cash": snapshot.get("total_cash"),
        "market_value": snapshot.get("total_market_value"),
        "position_count": len(positions) if isinstance(positions, list) else None,
        "recent_trade_count": len(status.get("recent_trades") or []),
    }


def validate_preconditions(status: Dict[str, Any], args: argparse.Namespace) -> None:
    failures = []
    if _gateway_name(status).upper() != "XTP":
        failures.append("gateway_is_not_xtp")
    if not _connection_confirmed(status):
        failures.append("xtp_connection_not_confirmed")
    if not _external_preflight_ok(status):
        failures.append("external_gateway_preflight_failed")
    settings = status.get("settings") if isinstance(status.get("settings"), dict) else {}
    if args.place_order and settings.get("auto_trade_enabled") is True:
        failures.append("auto_trade_must_be_disabled_during_acceptance")
    if _active_xtp_orders(status):
        failures.append("active_xtp_order_already_exists")

    if args.place_order:
        if args.confirmation != CONFIRMATION:
            failures.append("explicit_paper_order_confirmation_missing")
        if args.price is None or args.price <= 0:
            failures.append("positive_limit_price_required")
        if args.quantity <= 0 or args.quantity > MAX_QUANTITY:
            failures.append("quantity_exceeds_acceptance_limit")
        if args.quantity != int(args.quantity):
            failures.append("quantity_must_be_an_integer")
        if args.price is not None and args.quantity * args.price > MAX_NOTIONAL:
            failures.append("notional_exceeds_acceptance_limit")
        window = (status.get("diagnostics") or {}).get("trading_window") or {}
        if not args.allow_closed_market and window.get("is_market_open_now") is not True:
            failures.append("market_is_closed")

    if failures:
        raise AcceptanceError(",".join(failures))


def _poll_order(
    client: ApiClient,
    vt_orderid: str,
    *,
    timeout_seconds: float,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    deadline = time.monotonic() + timeout_seconds
    latest_status: Dict[str, Any] = {}
    observed: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        latest_status = client.get_status()
        observed = _find_order(latest_status, vt_orderid)
        if observed and str(observed.get("status") or "").lower() in TERMINAL_STATUSES:
            break
        time.sleep(1.0)
    return latest_status, observed


def run_acceptance(args: argparse.Namespace, client: ApiClient) -> Dict[str, Any]:
    before = client.get_status()
    validate_preconditions(before, args)
    report: Dict[str, Any] = {
        "gateway": _gateway_name(before),
        "connection_confirmed": _connection_confirmed(before),
        "external_preflight_ok": _external_preflight_ok(before),
        "places_orders": bool(args.place_order),
        "auto_trade_enabled": bool((before.get("settings") or {}).get("auto_trade_enabled")),
        "limits": {"max_quantity": MAX_QUANTITY, "max_notional": MAX_NOTIONAL},
        "before": _local_ledger_summary(before),
    }
    if not args.place_order:
        report["status"] = "preflight_passed"
        return report

    submitted = client.request(
        "POST",
        "/api/v1/vnpy-paper/orders",
        {
            "symbol": args.symbol,
            "side": "buy",
            "market": "cn",
            "quantity": int(args.quantity),
            "price": float(args.price),
            "note": "Controlled XTP paper acceptance",
            "execution_route": "vnpy_bridge",
        },
    )
    vt_orderid = str((submitted.get("raw") or {}).get("vt_orderid") or "")
    if submitted.get("accepted") is not True or not vt_orderid.upper().startswith("XTP."):
        raise AcceptanceError(f"XTP order submission was not accepted: {submitted.get('reason') or submitted.get('status')}")
    report["submission"] = {
        "accepted": True,
        "status": submitted.get("status"),
        "vt_orderid": vt_orderid,
        "symbol": submitted.get("symbol"),
        "quantity": submitted.get("quantity"),
        "price": submitted.get("price"),
        "cash_amount": submitted.get("cash_amount"),
    }

    latest, observed = _poll_order(client, vt_orderid, timeout_seconds=args.cancel_after_seconds)
    observed_status = str((observed or {}).get("status") or "").lower()
    if observed_status in ACTIVE_STATUSES:
        report["cancel_request"] = client.request(
            "POST",
            f"/api/v1/vnpy-paper/orders/{quote(vt_orderid, safe='')}/cancel",
            {"symbol": args.symbol, "market": "cn"},
        )
        latest, observed = _poll_order(client, vt_orderid, timeout_seconds=args.timeout_seconds)
        observed_status = str((observed or {}).get("status") or "").lower()

    report["order_callback"] = {
        "status": (observed or {}).get("status"),
        "traded": (observed or {}).get("traded"),
        "volume": (observed or {}).get("volume"),
        "rejected_reason": (observed or {}).get("rejected_reason"),
    }
    report["after"] = _local_ledger_summary(latest)
    report["status"] = "completed" if observed_status in TERMINAL_STATUSES else "order_not_terminal"
    if observed_status not in TERMINAL_STATUSES:
        raise AcceptanceError("XTP order did not reach a terminal state after cancellation")
    if args.require_fill and observed_status not in {"alltraded", "filled"}:
        raise AcceptanceError(f"XTP order was not filled: {observed_status}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--place-order", action="store_true")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--symbol", default="601668")
    parser.add_argument("--quantity", type=float, default=100)
    parser.add_argument("--price", type=float)
    parser.add_argument("--allow-closed-market", action="store_true")
    parser.add_argument("--require-fill", action="store_true")
    parser.add_argument("--cancel-after-seconds", type=float, default=20.0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report: Dict[str, Any]
    exit_code = 0
    try:
        client = ApiClient(args.base_url)
        report = run_acceptance(args, client)
    except AcceptanceError as exc:
        report = {"status": "failed", "error": str(exc)}
        exit_code = 1
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
