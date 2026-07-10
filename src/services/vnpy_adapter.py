# -*- coding: utf-8 -*-
"""Optional vn.py order mapping helpers.

This module intentionally keeps vn.py as an optional dependency. It exposes a
plain-dict OrderRequest payload that DSA can test without vn.py installed, and
only instantiates vn.py objects when the runtime already provides vn.py.
"""

from __future__ import annotations

import importlib
import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


class VnpyAdapterError(ValueError):
    """Raised when a DSA order cannot be mapped to vn.py semantics."""


_SSE_PREFIXES = ("5", "6", "9")
_SZSE_PREFIXES = ("0", "2", "3")
_EXCHANGE_ALIASES = {
    "SH": "SSE",
    "SSE": "SSE",
    "XSHG": "SSE",
    "SZ": "SZSE",
    "SZSE": "SZSE",
    "XSHE": "SZSE",
    "HK": "SEHK",
    "HKEX": "SEHK",
    "SEHK": "SEHK",
    "US": "SMART",
    "USA": "SMART",
    "SMART": "SMART",
}


def get_vnpy_adapter_status() -> Dict[str, Any]:
    """Return import and mapping diagnostics for the optional vn.py adapter."""

    imports: Dict[str, Dict[str, Any]] = {}
    object_module, imports["vnpy.trader.object"] = _try_import_module("vnpy.trader.object")
    constant_module, imports["vnpy.trader.constant"] = _try_import_module("vnpy.trader.constant")
    required_attrs = {
        "OrderRequest": getattr(object_module, "OrderRequest", None) if object_module is not None else None,
        "Exchange": getattr(constant_module, "Exchange", None) if constant_module is not None else None,
        "Direction": getattr(constant_module, "Direction", None) if constant_module is not None else None,
        "Offset": getattr(constant_module, "Offset", None) if constant_module is not None else None,
        "OrderType": getattr(constant_module, "OrderType", None) if constant_module is not None else None,
    }
    cancel_attrs = {
        "CancelRequest": getattr(object_module, "CancelRequest", None) if object_module is not None else None,
        "Exchange": getattr(constant_module, "Exchange", None) if constant_module is not None else None,
    }
    missing_attrs = [name for name, value in required_attrs.items() if value is None]
    cancel_missing_attrs = [name for name, value in cancel_attrs.items() if value is None]
    available = all(item.get("available") for item in imports.values()) and not missing_attrs

    return {
        "available": available,
        "mode": "vnpy_order_request" if available else "local_paper_fallback",
        "reason": None if available else _adapter_unavailable_reason(imports, missing_attrs),
        "imports": imports,
        "missing_attributes": missing_attrs,
        "cancel_request_missing_attributes": cancel_missing_attrs,
        "order_request_supported": available,
        "cancel_request_supported": all(item.get("available") for item in imports.values()) and not cancel_missing_attrs,
        "mapping": {
            "order_request_fields": [
                "symbol",
                "exchange",
                "direction",
                "offset",
                "type",
                "volume",
                "price",
                "reference",
            ],
            "cancel_request_fields": [
                "orderid",
                "symbol",
                "exchange",
            ],
            "supported_exchanges": ["SSE", "SZSE", "SEHK", "SMART"],
            "supported_directions": {"buy": "LONG", "sell": "SHORT"},
            "supported_order_types": ["LIMIT", "MARKET"],
            "stock_offset": "NONE",
            "cn_buy_lot_size": 100,
        },
    }


def get_vnpy_bridge_status(
    *,
    main_engine: Optional[Any] = None,
    gateway_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Return diagnostics for optional vn.py MainEngine order routing."""

    imports: Dict[str, Dict[str, Any]] = {}
    event_module, imports["vnpy.event"] = _try_import_module("vnpy.event")
    engine_module, imports["vnpy.trader.engine"] = _try_import_module("vnpy.trader.engine")
    event_engine_cls = getattr(event_module, "EventEngine", None) if event_module is not None else None
    main_engine_cls = getattr(engine_module, "MainEngine", None) if engine_module is not None else None
    missing_attrs = []
    if event_engine_cls is None:
        missing_attrs.append("EventEngine")
    if main_engine_cls is None:
        missing_attrs.append("MainEngine")

    send_order_supported = callable(getattr(main_engine, "send_order", None)) if main_engine is not None else False
    cancel_order_supported = callable(getattr(main_engine, "cancel_order", None)) if main_engine is not None else False
    gateway_configured = bool(str(gateway_name or "").strip())
    adapter_status = get_vnpy_adapter_status()
    order_request_supported = bool(adapter_status.get("order_request_supported"))
    available = bool(main_engine is not None and send_order_supported and gateway_configured and order_request_supported)
    reason = None
    if not available:
        if main_engine is None:
            reason = "main_engine_not_configured"
        elif not send_order_supported:
            reason = "send_order_unavailable"
        elif not gateway_configured:
            reason = "gateway_name_not_configured"
        elif not order_request_supported:
            reason = str(adapter_status.get("reason") or "order_request_unavailable")
        else:
            reason = _adapter_unavailable_reason(imports, missing_attrs)

    return {
        "available": available,
        "mode": "vnpy_main_engine" if available else "not_configured",
        "reason": reason,
        "imports": imports,
        "missing_attributes": missing_attrs,
        "gateway_name": str(gateway_name or "").strip() or None,
        "main_engine_configured": main_engine is not None,
        "send_order_supported": send_order_supported,
        "cancel_order_supported": cancel_order_supported,
        "order_request_supported": order_request_supported,
    }


def get_vnpy_event_bridge_status(*, event_engine: Optional[Any] = None) -> Dict[str, Any]:
    """Return diagnostics for optional vn.py EventEngine subscriptions."""

    event_module, event_import = _try_import_module("vnpy.trader.event")
    event_types = _resolve_vnpy_event_types(event_module)
    missing_event_types = [
        key for key in ("order", "trade", "account", "position") if not event_types.get(key)
    ]
    register_supported = callable(getattr(event_engine, "register", None)) if event_engine is not None else False
    unregister_supported = callable(getattr(event_engine, "unregister", None)) if event_engine is not None else False
    available = bool(event_engine is not None and register_supported and not missing_event_types)
    reason = None
    if not available:
        if event_engine is None:
            reason = "event_engine_not_configured"
        elif not register_supported:
            reason = "event_engine_register_unavailable"
        elif missing_event_types:
            reason = "missing_vnpy_event_types"
        elif not event_import.get("available"):
            reason = str(event_import.get("reason") or "event_module_unavailable")

    return {
        "available": available,
        "mode": "vnpy_event_engine" if available else "not_configured",
        "reason": reason,
        "imports": {"vnpy.trader.event": event_import},
        "event_engine_configured": event_engine is not None,
        "register_supported": register_supported,
        "unregister_supported": unregister_supported,
        "event_types": event_types,
        "missing_event_types": missing_event_types,
    }


class VnpyMainEngineBridge:
    """Small adapter around vn.py MainEngine.send_order."""

    def __init__(self, *, main_engine: Any, gateway_name: str) -> None:
        self.main_engine = main_engine
        self.gateway_name = str(gateway_name or "").strip()
        if not self.gateway_name:
            raise VnpyAdapterError("vn.py gateway_name is required")
        if not callable(getattr(main_engine, "send_order", None)):
            raise VnpyAdapterError("vn.py main_engine must provide send_order")

    def send_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a vn.py OrderRequest and submit it through MainEngine."""

        request = create_vnpy_order_request(payload)
        vt_orderid = self.main_engine.send_order(request, self.gateway_name)
        if not vt_orderid:
            raise VnpyAdapterError("vn.py MainEngine returned empty vt_orderid")
        return {
            "accepted": True,
            "status": "submitted",
            "vt_orderid": str(vt_orderid),
            "gateway_name": self.gateway_name,
            "order_request": request,
            "order_request_payload": payload,
        }

    def cancel_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a vn.py CancelRequest and submit it through MainEngine."""

        cancel_order = getattr(self.main_engine, "cancel_order", None)
        if not callable(cancel_order):
            raise VnpyAdapterError("vn.py main_engine must provide cancel_order")
        request = create_vnpy_cancel_request(payload)
        raw_result = cancel_order(request, self.gateway_name)
        return {
            "accepted": True,
            "status": "cancel_requested",
            "vt_orderid": str(payload.get("vt_orderid") or "").strip() or None,
            "gateway_name": self.gateway_name,
            "cancel_request": request,
            "cancel_request_payload": payload,
            "raw_result": raw_result,
        }


class VnpyEventSubscriptionBridge:
    """Register DSA callbacks on a vn.py EventEngine-like object."""

    def __init__(
        self,
        *,
        event_engine: Any,
        order_handler: Optional[Callable[[Any], None]] = None,
        trade_handler: Optional[Callable[[Any], None]] = None,
        account_handler: Optional[Callable[[Any], None]] = None,
        position_handler: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.event_engine = event_engine
        self.handlers = {
            "order": order_handler,
            "trade": trade_handler,
            "account": account_handler,
            "position": position_handler,
        }
        self.event_types = _resolve_vnpy_event_types(_try_import_module("vnpy.trader.event")[0])
        self._registered: List[Tuple[str, Callable[[Any], None]]] = []
        if not callable(getattr(event_engine, "register", None)):
            raise VnpyAdapterError("vn.py event_engine must provide register")

    def register(self) -> Dict[str, Any]:
        """Register configured callbacks once and return subscription diagnostics."""

        if self._registered:
            return self.status()
        for key, handler in self.handlers.items():
            event_type = self.event_types.get(key)
            if not event_type or handler is None:
                continue
            self.event_engine.register(event_type, handler)
            self._registered.append((event_type, handler))
        return self.status()

    def unregister(self) -> Dict[str, Any]:
        """Best-effort unregister of previously registered callbacks."""

        unregister = getattr(self.event_engine, "unregister", None)
        if not callable(unregister):
            self._registered = []
            return self.status()
        for event_type, handler in list(self._registered):
            unregister(event_type, handler)
        self._registered = []
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "registered": bool(self._registered),
            "registered_count": len(self._registered),
            "event_types": [event_type for event_type, _handler in self._registered],
        }


def build_vnpy_order_request_payload(
    *,
    symbol: str,
    side: str = "buy",
    market: str = "cn",
    quantity: Optional[float] = None,
    cash_amount: Optional[float] = None,
    price: Optional[float] = None,
    source: str = "dsa",
    plan_uid: Optional[str] = None,
    reference: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a DSA paper order into a vn.py OrderRequest-shaped dict."""

    normalized_symbol, exchange = normalize_vnpy_symbol(symbol, market=market)
    side_norm = str(side or "").strip().lower()
    if side_norm not in {"buy", "sell"}:
        raise VnpyAdapterError("side must be buy or sell")

    price_value = _safe_positive_float(price)
    volume = _resolve_volume(
        market=market,
        side=side_norm,
        quantity=quantity,
        cash_amount=cash_amount,
        price=price_value,
    )
    order_type = "LIMIT" if price_value is not None and price_value > 0 else "MARKET"
    order_reference = reference or _build_reference(source=source, plan_uid=plan_uid)
    payload = {
        "symbol": normalized_symbol,
        "exchange": exchange,
        "vt_symbol": f"{normalized_symbol}.{exchange}",
        "direction": "LONG" if side_norm == "buy" else "SHORT",
        "offset": "NONE",
        "type": order_type,
        "volume": volume,
        "price": price_value or 0.0,
        "reference": order_reference,
        "market": str(market or "").strip().lower() or None,
        "side": side_norm,
        "source": str(source or "dsa").strip() or "dsa",
    }
    if plan_uid:
        payload["plan_uid"] = str(plan_uid)
    if cash_amount is not None:
        payload["cash_amount"] = float(cash_amount)
    return payload


def build_vnpy_cancel_request_payload(
    *,
    vt_orderid: str,
    symbol: str,
    market: str = "cn",
    order_id: Optional[str] = None,
    exchange: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a DSA submitted order into a vn.py CancelRequest-shaped dict."""

    vt_orderid_norm = str(vt_orderid or "").strip()
    if not vt_orderid_norm:
        raise VnpyAdapterError("vt_orderid is required")

    normalized_symbol, inferred_exchange = normalize_vnpy_symbol(symbol, market=market)
    order_id_norm = str(order_id or "").strip() or _extract_order_id(vt_orderid_norm)
    if not order_id_norm:
        raise VnpyAdapterError("orderid is required")
    exchange_norm = str(exchange or "").strip().upper()
    if exchange_norm:
        exchange_norm = _EXCHANGE_ALIASES.get(exchange_norm, exchange_norm)
    else:
        exchange_norm = inferred_exchange

    return {
        "vt_orderid": vt_orderid_norm,
        "orderid": order_id_norm,
        "symbol": normalized_symbol,
        "exchange": exchange_norm,
        "vt_symbol": f"{normalized_symbol}.{exchange_norm}",
        "market": str(market or "").strip().lower() or None,
    }


def create_vnpy_order_request(payload: Dict[str, Any]) -> Any:
    """Instantiate vn.py.trader.object.OrderRequest from a mapped payload."""

    modules = _load_vnpy_modules()
    object_module = modules["object"]
    constant_module = modules["constant"]
    order_request_cls = getattr(object_module, "OrderRequest")
    return order_request_cls(
        symbol=payload["symbol"],
        exchange=_enum_member(getattr(constant_module, "Exchange"), payload["exchange"]),
        direction=_enum_member(getattr(constant_module, "Direction"), payload["direction"]),
        offset=_enum_member(getattr(constant_module, "Offset"), payload.get("offset") or "NONE"),
        type=_enum_member(getattr(constant_module, "OrderType"), payload["type"]),
        volume=payload["volume"],
        price=payload.get("price") or 0.0,
        reference=payload.get("reference") or "",
    )


def create_vnpy_cancel_request(payload: Dict[str, Any]) -> Any:
    """Instantiate vn.py.trader.object.CancelRequest from a mapped payload."""

    modules = _load_vnpy_modules()
    object_module = modules["object"]
    constant_module = modules["constant"]
    if not hasattr(object_module, "CancelRequest"):
        raise VnpyAdapterError("vn.py module is missing CancelRequest")
    cancel_request_cls = getattr(object_module, "CancelRequest")
    return cancel_request_cls(
        orderid=payload["orderid"],
        symbol=payload["symbol"],
        exchange=_enum_member(getattr(constant_module, "Exchange"), payload["exchange"]),
    )


def normalize_vnpy_symbol(symbol: str, *, market: str = "cn") -> Tuple[str, str]:
    """Normalize common DSA stock symbols into vn.py symbol/exchange parts."""

    raw_symbol = str(symbol or "").strip().upper()
    if not raw_symbol:
        raise VnpyAdapterError("symbol is required")

    cleaned = raw_symbol.replace("_", ".").replace("-", ".")
    prefix_match = re.match(r"^(SH|SZ|HK|US)([A-Z0-9.]+)$", cleaned)
    suffix_match = re.match(r"^([A-Z0-9]+)\.([A-Z]+)$", cleaned)
    if prefix_match:
        exchange = _EXCHANGE_ALIASES.get(prefix_match.group(1), prefix_match.group(1))
        normalized_symbol = prefix_match.group(2)
    elif suffix_match and suffix_match.group(2) in _EXCHANGE_ALIASES:
        normalized_symbol = suffix_match.group(1)
        exchange = _EXCHANGE_ALIASES[suffix_match.group(2)]
    else:
        normalized_symbol = cleaned
        exchange = _infer_exchange(normalized_symbol, market)

    if exchange == "SEHK" and normalized_symbol.isdigit():
        normalized_symbol = normalized_symbol.zfill(5)
    return normalized_symbol, exchange


def _try_import_module(module_name: str) -> Tuple[Optional[Any], Dict[str, Any]]:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        return None, {
            "available": False,
            "reason": "missing_module",
            "module": module_name,
            "missing_module": exc.name,
        }
    except Exception as exc:  # noqa: BLE001 - status diagnostics must be best-effort.
        return None, {
            "available": False,
            "reason": "import_error",
            "module": module_name,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    return module, {"available": True, "module": module_name}


def _adapter_unavailable_reason(imports: Dict[str, Dict[str, Any]], missing_attrs: list[str]) -> str:
    for item in imports.values():
        reason = item.get("reason")
        if reason:
            return str(reason)
    if missing_attrs:
        return "missing_vnpy_attributes"
    return "vnpy_unavailable"


def _resolve_vnpy_event_types(event_module: Optional[Any]) -> Dict[str, Optional[str]]:
    return {
        "order": _event_type_value(event_module, "EVENT_ORDER"),
        "trade": _event_type_value(event_module, "EVENT_TRADE"),
        "account": _event_type_value(event_module, "EVENT_ACCOUNT"),
        "position": _event_type_value(event_module, "EVENT_POSITION"),
    }


def _event_type_value(event_module: Optional[Any], attr_name: str) -> Optional[str]:
    if event_module is None:
        return None
    value = getattr(event_module, attr_name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_vnpy_modules() -> Dict[str, Any]:
    object_module = importlib.import_module("vnpy.trader.object")
    constant_module = importlib.import_module("vnpy.trader.constant")
    for attr_name, module in [
        ("OrderRequest", object_module),
        ("Exchange", constant_module),
        ("Direction", constant_module),
        ("Offset", constant_module),
        ("OrderType", constant_module),
    ]:
        if not hasattr(module, attr_name):
            raise VnpyAdapterError(f"vn.py module is missing {attr_name}")
    return {"object": object_module, "constant": constant_module}


def _enum_member(enum_cls: Any, name: str) -> Any:
    try:
        return getattr(enum_cls, str(name))
    except AttributeError as exc:
        raise VnpyAdapterError(f"vn.py enum {enum_cls!r} is missing {name}") from exc


def _infer_exchange(symbol: str, market: str) -> str:
    market_norm = str(market or "").strip().lower()
    if market_norm == "cn":
        if symbol.startswith(_SSE_PREFIXES):
            return "SSE"
        if symbol.startswith(_SZSE_PREFIXES):
            return "SZSE"
        raise VnpyAdapterError(f"cannot infer A-share exchange for {symbol}")
    if market_norm == "hk":
        return "SEHK"
    if market_norm == "us":
        return "SMART"
    return _EXCHANGE_ALIASES.get(market_norm.upper(), market_norm.upper() or "SMART")


def _resolve_volume(
    *,
    market: str,
    side: str,
    quantity: Optional[float],
    cash_amount: Optional[float],
    price: Optional[float],
) -> float:
    quantity_value = _safe_positive_float(quantity)
    if quantity_value is None:
        cash_value = _safe_positive_float(cash_amount)
        if cash_value is None or price is None or price <= 0:
            raise VnpyAdapterError("quantity or cash_amount with price is required")
        quantity_value = cash_value / price

    if str(market or "").strip().lower() == "cn" and side == "buy":
        quantity_value = math.floor(quantity_value / 100.0) * 100.0
    if quantity_value <= 0:
        raise VnpyAdapterError("resolved volume is zero")
    return round(float(quantity_value), 8)


def _safe_positive_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _build_reference(*, source: str, plan_uid: Optional[str]) -> str:
    source_norm = str(source or "dsa").strip() or "dsa"
    if plan_uid:
        return f"dsa:{source_norm}:{plan_uid}"
    return f"dsa:{source_norm}"


def _extract_order_id(vt_orderid: str) -> str:
    text = str(vt_orderid or "").strip()
    if "." not in text:
        return text
    return text.rsplit(".", 1)[-1].strip()
