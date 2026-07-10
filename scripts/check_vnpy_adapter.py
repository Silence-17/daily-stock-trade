# -*- coding: utf-8 -*-
"""Smoke-check the optional vn.py adapter without requiring vn.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.vnpy_adapter import (  # noqa: E402
    VnpyAdapterError,
    build_vnpy_order_request_payload,
    create_vnpy_order_request,
    get_vnpy_adapter_status,
)


def main() -> int:
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
        "vnpy_adapter": status,
        "sample_order_request_payload": payload,
    }
    if status.get("available"):
        try:
            order_request = create_vnpy_order_request(payload)
            result["order_request_class"] = (
                f"{order_request.__class__.__module__}.{order_request.__class__.__name__}"
            )
        except Exception as exc:  # noqa: BLE001 - script should print actionable diagnostics.
            result["ok"] = False
            result["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
    else:
        result["fallback"] = "vnpy is not importable; DSA local paper mode remains usable."

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
