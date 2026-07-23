from argparse import Namespace
from unittest.mock import MagicMock

import pytest

from scripts.check_vnpy_xtp_order_acceptance import (
    AcceptanceError,
    CONFIRMATION,
    run_acceptance,
    validate_preconditions,
)


def _status(*, market_open=True, active_orders=None):
    return {
        "settings": {"auto_trade_enabled": False, "vnpy_gateway_name": "XTP"},
        "account": {"id": 8},
        "snapshot": {
            "total_cash": 100000.0,
            "total_market_value": 0.0,
            "accounts": [{"positions": []}],
        },
        "recent_trades": [],
        "diagnostics": {
            "trading_window": {"is_market_open_now": market_open},
            "vnpy_runtime": {
                "gateway": {"gateway_name": "XTP"},
                "connect": {"connected": True},
                "production_preflight": {"ok": True, "external_gateway": True},
            },
            "vnpy_sync_state": {"recent_orders": active_orders or []},
        },
    }


def _args(**overrides):
    values = {
        "place_order": False,
        "confirmation": "",
        "symbol": "601668",
        "quantity": 100,
        "price": None,
        "allow_closed_market": False,
        "require_fill": False,
        "cancel_after_seconds": 0,
        "timeout_seconds": 0,
    }
    values.update(overrides)
    return Namespace(**values)


def test_preflight_only_never_places_order():
    client = MagicMock()
    client.get_status.return_value = _status()

    report = run_acceptance(_args(), client)

    assert report["status"] == "preflight_passed"
    assert report["places_orders"] is False
    client.request.assert_not_called()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"confirmation": ""}, "explicit_paper_order_confirmation_missing"),
        ({"quantity": 200}, "quantity_exceeds_acceptance_limit"),
        ({"price": 25.0}, "notional_exceeds_acceptance_limit"),
    ],
)
def test_order_safety_limits_fail_closed(overrides, reason):
    values = {
        "place_order": True,
        "confirmation": CONFIRMATION,
        "price": 4.7,
        **overrides,
    }
    args = _args(**values)

    with pytest.raises(AcceptanceError, match=reason):
        validate_preconditions(_status(), args)


def test_existing_active_xtp_order_blocks_new_acceptance_order():
    args = _args(place_order=True, confirmation=CONFIRMATION, price=4.7)
    status = _status(active_orders=[{"vt_orderid": "XTP.1", "status": "nottraded"}])

    with pytest.raises(AcceptanceError, match="active_xtp_order_already_exists"):
        validate_preconditions(status, args)
