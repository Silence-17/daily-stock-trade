# -*- coding: utf-8 -*-
"""Optional vn.py runtime bootstrap helpers.

The runtime stays opt-in. When disabled or unavailable, DSA keeps using the
local paper ledger and exposes diagnostics instead of failing app startup.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.services.vnpy_paper_trading_service import VnpyPaperTradingService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VnpyRuntimeSettings:
    enabled: bool = False
    gateway_class: Optional[str] = None
    gateway_name: Optional[str] = None
    connect_settings_path: Optional[str] = None
    connect_on_start: bool = False
    auto_attach_events: bool = True


class VnpyRuntimeHandle:
    """Owns optional vn.py runtime resources created by DSA."""

    def __init__(
        self,
        *,
        settings: VnpyRuntimeSettings,
        event_engine: Optional[Any] = None,
        main_engine: Optional[Any] = None,
        event_bridge: Optional[Any] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.settings = settings
        self.event_engine = event_engine
        self.main_engine = main_engine
        self.event_bridge = event_bridge
        self.diagnostics = diagnostics or {}

    def close(self) -> None:
        """Release created vn.py resources best-effort."""

        if callable(getattr(self.event_bridge, "unregister", None)):
            try:
                self.event_bridge.unregister()
            except Exception as exc:  # noqa: BLE001 - shutdown must be best-effort.
                logger.warning("Failed to unregister vn.py event bridge: %s", exc)
        if callable(getattr(self.main_engine, "close", None)):
            try:
                self.main_engine.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to close vn.py MainEngine: %s", exc)
        if callable(getattr(self.event_engine, "stop", None)):
            try:
                self.event_engine.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to stop vn.py EventEngine: %s", exc)


def load_vnpy_runtime_settings() -> VnpyRuntimeSettings:
    """Load optional vn.py runtime settings from environment variables."""

    return VnpyRuntimeSettings(
        enabled=_env_bool("VNPY_RUNTIME_ENABLED", default=False),
        gateway_class=_env_text("VNPY_GATEWAY_CLASS"),
        gateway_name=_env_text("VNPY_GATEWAY_NAME"),
        connect_settings_path=_env_text("VNPY_CONNECT_SETTINGS_PATH"),
        connect_on_start=_env_bool("VNPY_CONNECT_ON_START", default=False),
        auto_attach_events=_env_bool("VNPY_AUTO_ATTACH_EVENTS", default=True),
    )


def bootstrap_vnpy_runtime(
    *,
    settings: Optional[VnpyRuntimeSettings] = None,
    config_path: Optional[Path] = None,
) -> VnpyRuntimeHandle:
    """Create an optional vn.py MainEngine/EventEngine runtime."""

    settings = settings or load_vnpy_runtime_settings()
    diagnostics: Dict[str, Any] = {
        "enabled": settings.enabled,
        "gateway_class": settings.gateway_class,
        "gateway_name": settings.gateway_name,
        "connect_on_start": settings.connect_on_start,
        "auto_attach_events": settings.auto_attach_events,
        "mode": "disabled",
        "available": False,
        "reason": "disabled",
    }
    if not settings.enabled:
        return VnpyRuntimeHandle(settings=settings, diagnostics=diagnostics)

    try:
        runtime_data_dir = Path.cwd().joinpath(".vntrader")
        runtime_data_dir.mkdir(parents=True, exist_ok=True)
        diagnostics["runtime_data_dir"] = str(runtime_data_dir)
        event_module = importlib.import_module("vnpy.event")
        engine_module = importlib.import_module("vnpy.trader.engine")
        event_engine_cls = getattr(event_module, "EventEngine")
        main_engine_cls = getattr(engine_module, "MainEngine")
        event_engine = event_engine_cls()
        main_engine = main_engine_cls(event_engine)
        diagnostics.update(
            {
                "mode": "vnpy_runtime",
                "available": True,
                "reason": None,
                "event_engine_created": True,
                "main_engine_created": True,
            }
        )
    except Exception as exc:  # noqa: BLE001 - enabled runtime should degrade startup, not crash DSA.
        diagnostics.update(
            {
                "mode": "unavailable",
                "available": False,
                "reason": "bootstrap_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        logger.warning("vn.py runtime bootstrap failed: %s", exc)
        return VnpyRuntimeHandle(settings=settings, diagnostics=diagnostics)

    if settings.gateway_class:
        _add_gateway(
            main_engine=main_engine,
            gateway_class_path=settings.gateway_class,
            gateway_name=settings.gateway_name,
            diagnostics=diagnostics,
        )

    if settings.connect_on_start:
        _connect_gateway(
            main_engine=main_engine,
            gateway_name=settings.gateway_name,
            settings_path=settings.connect_settings_path,
            diagnostics=diagnostics,
        )

    event_bridge = None
    if settings.auto_attach_events:
        try:
            service = VnpyPaperTradingService(
                config_path=config_path,
                vnpy_main_engine=main_engine,
                vnpy_event_engine=event_engine,
            )
            event_bridge = service.attach_vnpy_event_engine(event_engine)
            diagnostics["event_bridge"] = event_bridge.status()
        except Exception as exc:  # noqa: BLE001 - order routing can still be diagnosed without event attach.
            diagnostics["event_bridge"] = {
                "registered": False,
                "reason": "attach_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            logger.warning("vn.py EventEngine attach failed: %s", exc)

    return VnpyRuntimeHandle(
        settings=settings,
        event_engine=event_engine,
        main_engine=main_engine,
        event_bridge=event_bridge,
        diagnostics=diagnostics,
    )


def _add_gateway(
    *,
    main_engine: Any,
    gateway_class_path: str,
    gateway_name: Optional[str],
    diagnostics: Dict[str, Any],
) -> None:
    gateway_cls = _import_object(gateway_class_path)
    add_gateway = getattr(main_engine, "add_gateway", None)
    if not callable(add_gateway):
        diagnostics["gateway"] = {
            "added": False,
            "reason": "add_gateway_unavailable",
        }
        return
    try:
        if gateway_name:
            add_gateway(gateway_cls, gateway_name)
        else:
            add_gateway(gateway_cls)
    except TypeError:
        add_gateway(gateway_cls)
    diagnostics["gateway"] = {
        "added": True,
        "class": gateway_class_path,
        "gateway_name": gateway_name,
    }


def _connect_gateway(
    *,
    main_engine: Any,
    gateway_name: Optional[str],
    settings_path: Optional[str],
    diagnostics: Dict[str, Any],
) -> None:
    connect = getattr(main_engine, "connect", None)
    if not callable(connect):
        diagnostics["connect"] = {
            "attempted": True,
            "connected": False,
            "reason": "connect_unavailable",
        }
        return
    if not gateway_name:
        diagnostics["connect"] = {
            "attempted": True,
            "connected": False,
            "reason": "gateway_name_required",
        }
        return
    path: Optional[Path] = None
    if settings_path:
        path = Path(settings_path).expanduser()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            diagnostics["connect"] = {
                "attempted": True,
                "connected": False,
                "reason": "connect_settings_read_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            return
    else:
        get_gateway = getattr(main_engine, "get_gateway", None)
        gateway = get_gateway(gateway_name) if callable(get_gateway) else None
        if not bool(getattr(gateway, "connect_without_settings", False)):
            diagnostics["connect"] = {
                "attempted": True,
                "connected": False,
                "reason": "connect_settings_path_required",
            }
            return
        payload = {}
    if not isinstance(payload, dict):
        diagnostics["connect"] = {
            "attempted": True,
            "connected": False,
            "reason": "connect_settings_must_be_object",
        }
        return
    connect(payload, gateway_name)
    diagnostics["connect"] = {
        "attempted": True,
        "connected": True,
        "settings_path": str(path) if path is not None else None,
        "settings_source": "file" if path is not None else "gateway_defaults",
        "gateway_name": gateway_name,
    }


def _import_object(path: str) -> Any:
    text = str(path or "").strip()
    if ":" in text:
        module_name, attr_name = text.split(":", 1)
    else:
        module_name, _, attr_name = text.rpartition(".")
    if not module_name or not attr_name:
        raise ValueError("gateway class must be module:Class or module.Class")
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _env_text(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
