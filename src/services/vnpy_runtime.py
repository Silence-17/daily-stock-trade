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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Event, RLock, Thread
from typing import Any, Callable, Dict, Optional, Tuple

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
    auto_reconnect_enabled: bool = False
    auto_reconnect_interval_seconds: int = 60
    auto_reconnect_max_interval_seconds: int = 300
    auto_reconnect_confirmation_grace_seconds: int = 30


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
        event_sink: Optional[Callable[..., None]] = None,
    ) -> None:
        self.settings = settings
        self.event_engine = event_engine
        self.main_engine = main_engine
        self.event_bridge = event_bridge
        self.diagnostics = diagnostics or {}
        self._diagnostics_lock = RLock()
        self._auto_reconnect_stop = Event()
        self._auto_reconnect_thread: Optional[Thread] = None
        self._event_sink = event_sink
        self._event_queue: Queue[Optional[Dict[str, Any]]] = Queue()
        self._event_thread: Optional[Thread] = None

    def refresh_diagnostics(self) -> Dict[str, Any]:
        """Refresh gateway state exposed by runtimes with a status hook."""

        with self._diagnostics_lock:
            _refresh_gateway_connection(
                main_engine=self.main_engine,
                gateway_name=self.settings.gateway_name,
                diagnostics=self.diagnostics,
            )
            if callable(getattr(self.event_bridge, "status", None)):
                self.diagnostics["event_bridge"] = self.event_bridge.status()
            return self.diagnostics

    def start_auto_reconnect(self) -> None:
        """Start the opt-in gateway reconnect monitor."""

        with self._diagnostics_lock:
            state = self.diagnostics.setdefault("auto_reconnect", {})
            state.update(
                {
                    "enabled": self.settings.auto_reconnect_enabled,
                    "interval_seconds": self.settings.auto_reconnect_interval_seconds,
                    "max_interval_seconds": _auto_reconnect_max_interval(
                        self.settings
                    ),
                    "confirmation_grace_seconds": (
                        self.settings.auto_reconnect_confirmation_grace_seconds
                    ),
                    "running": False,
                }
            )
            state.setdefault("attempt_count", 0)
            state.setdefault("success_count", 0)
            state.setdefault("failure_count", 0)
            state.setdefault("consecutive_failure_count", 0)
            state.setdefault(
                "current_interval_seconds",
                self.settings.auto_reconnect_interval_seconds,
            )
            if not self.settings.auto_reconnect_enabled:
                state["reason"] = "disabled"
                return
            if (
                not self.settings.enabled
                or not self.settings.connect_on_start
                or self.main_engine is None
                or not self.settings.gateway_name
            ):
                state["reason"] = "runtime_connect_not_configured"
                return
            if self._auto_reconnect_thread is not None and self._auto_reconnect_thread.is_alive():
                state["running"] = True
                state["reason"] = None
                return
            self._auto_reconnect_stop.clear()
            state["reason"] = None
            state["running"] = True
            state["next_check_at"] = _future_iso(
                self.settings.auto_reconnect_interval_seconds
            )
            thread = Thread(
                target=self._auto_reconnect_loop,
                name="vnpy-auto-reconnect",
                daemon=True,
            )
            self._auto_reconnect_thread = thread
            thread.start()

    def run_auto_reconnect_check(self) -> Dict[str, Any]:
        """Run one deterministic reconnect check for the monitor and tests."""

        return self._run_reconnect_check(
            require_auto_enabled=True,
            trigger="monitor",
        )

    def run_manual_reconnect(self) -> Dict[str, Any]:
        """Attempt one operator-requested reconnect when it is safe to do so."""

        return self._run_reconnect_check(
            require_auto_enabled=False,
            trigger="manual",
        )

    def _run_reconnect_check(
        self,
        *,
        require_auto_enabled: bool,
        trigger: str,
    ) -> Dict[str, Any]:
        """Run one serialized reconnect check for the monitor or an operator."""

        with self._diagnostics_lock:
            state = self.diagnostics.setdefault("auto_reconnect", {})
            state["last_check_at"] = _utc_iso()
            state["last_trigger"] = trigger
            if require_auto_enabled and not self.settings.auto_reconnect_enabled:
                state["last_result"] = "disabled"
                state["last_reason"] = "disabled"
                return state
            if (
                not self.settings.enabled
                or self.main_engine is None
                or not self.settings.gateway_name
            ):
                state["last_check_result"] = "unavailable"
                state["last_check_reason"] = "runtime_connect_not_configured"
                return state
            _refresh_gateway_connection(
                main_engine=self.main_engine,
                gateway_name=self.settings.gateway_name,
                diagnostics=self.diagnostics,
            )
            connect = self.diagnostics.get("connect")
            status = connect.get("status") if isinstance(connect, dict) else None
            reconnectable_statuses = {"failed", "disconnected"}
            if trigger == "manual" and status is None:
                reconnectable_statuses.add(None)
            if status not in reconnectable_statuses:
                state["last_check_result"] = "not_required"
                state["last_check_reason"] = status or "connect_status_unavailable"
                if status == "connected":
                    _reset_auto_reconnect_backoff(state, self.settings)
                return state
            if (
                status == "disconnected"
                and isinstance(connect, dict)
                and connect.get("request_accepted") is True
                and not connect.get("confirmed_at")
                and _within_confirmation_grace(
                    connect.get("request_accepted_at"),
                    self.settings.auto_reconnect_confirmation_grace_seconds,
                )
            ):
                state["last_check_result"] = "confirmation_pending"
                state["last_check_reason"] = "connection_confirmation_grace"
                return state

            state["attempt_count"] = int(state.get("attempt_count") or 0) + 1
            state["last_attempt_at"] = _utc_iso()
            state["last_check_result"] = "reconnect_attempted"
            state["last_check_reason"] = status
            _connect_gateway(
                main_engine=self.main_engine,
                gateway_name=self.settings.gateway_name,
                settings_path=self.settings.connect_settings_path,
                diagnostics=self.diagnostics,
            )
            _refresh_gateway_connection(
                main_engine=self.main_engine,
                gateway_name=self.settings.gateway_name,
                diagnostics=self.diagnostics,
            )
            connect = self.diagnostics.get("connect")
            connected = bool(
                isinstance(connect, dict) and connect.get("connected") is True
            )
            if connected:
                recovered_after_failures = int(
                    state.get("consecutive_failure_count") or 0
                )
                state["success_count"] = int(state.get("success_count") or 0) + 1
                state["last_result"] = "reconnected"
                state["last_reason"] = None
                state["last_success_at"] = _utc_iso()
                _reset_auto_reconnect_backoff(state, self.settings)
                self._enqueue_reconnect_event(
                    event_type="vnpy_gateway_reconnected",
                    status="resolved",
                    reason="vnpy_gateway_reconnected",
                    observed_value=0,
                    threshold=recovered_after_failures or None,
                    reconnect_state=state,
                    connect_state=connect,
                )
            else:
                previous_failures = int(
                    state.get("consecutive_failure_count") or 0
                )
                previous_interval = state.get("current_interval_seconds")
                state["failure_count"] = int(state.get("failure_count") or 0) + 1
                state["last_result"] = "failed"
                state["last_reason"] = (
                    connect.get("reason")
                    if isinstance(connect, dict)
                    else "connect_status_unavailable"
                )
                _increase_auto_reconnect_backoff(state, self.settings)
                if previous_failures == 0:
                    self._enqueue_reconnect_event(
                        event_type="vnpy_gateway_reconnect_failed",
                        status="failed",
                        reason=str(state.get("last_reason") or "reconnect_failed"),
                        observed_value=state.get("consecutive_failure_count"),
                        threshold=None,
                        reconnect_state=state,
                        connect_state=connect,
                    )
                if (
                    state.get("current_interval_seconds")
                    == state.get("max_interval_seconds")
                    and previous_interval != state.get("max_interval_seconds")
                ):
                    self._enqueue_reconnect_event(
                        event_type="vnpy_gateway_reconnect_backoff_capped",
                        status="degraded",
                        reason="vnpy_gateway_reconnect_backoff_capped",
                        observed_value=state.get("current_interval_seconds"),
                        threshold=state.get("max_interval_seconds"),
                        reconnect_state=state,
                        connect_state=connect,
                    )
            return state

    def _enqueue_reconnect_event(
        self,
        *,
        event_type: str,
        status: str,
        reason: str,
        observed_value: Optional[Any],
        threshold: Optional[Any],
        reconnect_state: Dict[str, Any],
        connect_state: Any,
    ) -> None:
        if self._event_sink is None:
            return
        if self._event_thread is None or not self._event_thread.is_alive():
            self._event_thread = Thread(
                target=self._reconnect_event_loop,
                name="vnpy-reconnect-events",
                daemon=True,
            )
            self._event_thread.start()
        connect = connect_state if isinstance(connect_state, dict) else {}
        self._event_queue.put(
            {
                "event_type": event_type,
                "status": status,
                "reason": reason,
                "observed_value": observed_value,
                "threshold": threshold,
                "diagnostics": {
                    "gateway_name": self.settings.gateway_name,
                    "trigger": reconnect_state.get("last_trigger"),
                    "connect_status": connect.get("status"),
                    "confirmation_source": connect.get("confirmation_source"),
                    "consecutive_failure_count": reconnect_state.get(
                        "consecutive_failure_count"
                    ),
                    "current_interval_seconds": reconnect_state.get(
                        "current_interval_seconds"
                    ),
                },
            }
        )

    def _reconnect_event_loop(self) -> None:
        while True:
            event = self._event_queue.get()
            try:
                if event is None:
                    return
                try:
                    self._event_sink(**event)
                except Exception as exc:  # noqa: BLE001 - event history cannot block reconnect.
                    logger.warning("Failed to record vn.py reconnect event: %s", exc)
            finally:
                self._event_queue.task_done()

    def _auto_reconnect_loop(self) -> None:
        try:
            while True:
                with self._diagnostics_lock:
                    state = self.diagnostics.setdefault("auto_reconnect", {})
                    interval = state.get(
                        "current_interval_seconds",
                        self.settings.auto_reconnect_interval_seconds,
                    )
                    state["next_check_at"] = _future_iso(interval)
                if self._auto_reconnect_stop.wait(interval):
                    break
                self.run_auto_reconnect_check()
        finally:
            with self._diagnostics_lock:
                state = self.diagnostics.setdefault("auto_reconnect", {})
                state["running"] = False
                state["next_check_at"] = None

    def close(self) -> None:
        """Release created vn.py resources best-effort."""

        self._auto_reconnect_stop.set()
        thread = self._auto_reconnect_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        event_thread = self._event_thread
        if event_thread is not None and event_thread.is_alive():
            self._event_queue.put(None)
            event_thread.join(timeout=5)

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
        auto_reconnect_enabled=_env_bool("VNPY_AUTO_RECONNECT_ENABLED", default=False),
        auto_reconnect_interval_seconds=_env_int(
            "VNPY_AUTO_RECONNECT_INTERVAL_SECONDS",
            default=60,
            minimum=5,
            maximum=3600,
        ),
        auto_reconnect_max_interval_seconds=_env_int(
            "VNPY_AUTO_RECONNECT_MAX_INTERVAL_SECONDS",
            default=300,
            minimum=5,
            maximum=3600,
        ),
        auto_reconnect_confirmation_grace_seconds=_env_int(
            "VNPY_AUTO_RECONNECT_CONFIRMATION_GRACE_SECONDS",
            default=30,
            minimum=5,
            maximum=600,
        ),
    )


def bootstrap_vnpy_runtime(
    *,
    settings: Optional[VnpyRuntimeSettings] = None,
    config_path: Optional[Path] = None,
    event_sink: Optional[Callable[..., None]] = None,
) -> VnpyRuntimeHandle:
    """Create an optional vn.py MainEngine/EventEngine runtime."""

    settings = settings or load_vnpy_runtime_settings()
    diagnostics: Dict[str, Any] = {
        "enabled": settings.enabled,
        "gateway_class": settings.gateway_class,
        "gateway_name": settings.gateway_name,
        "connect_on_start": settings.connect_on_start,
        "auto_attach_events": settings.auto_attach_events,
        "auto_reconnect": {
            "enabled": settings.auto_reconnect_enabled,
            "interval_seconds": settings.auto_reconnect_interval_seconds,
            "max_interval_seconds": _auto_reconnect_max_interval(settings),
            "current_interval_seconds": settings.auto_reconnect_interval_seconds,
            "consecutive_failure_count": 0,
            "confirmation_grace_seconds": (
                settings.auto_reconnect_confirmation_grace_seconds
            ),
            "running": False,
            "reason": "disabled" if not settings.auto_reconnect_enabled else None,
        },
        "mode": "disabled",
        "available": False,
        "reason": "disabled",
    }
    if not settings.enabled:
        handle = VnpyRuntimeHandle(
            settings=settings,
            diagnostics=diagnostics,
            event_sink=event_sink,
        )
        handle.start_auto_reconnect()
        return handle

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
        handle = VnpyRuntimeHandle(
            settings=settings,
            diagnostics=diagnostics,
            event_sink=event_sink,
        )
        handle.start_auto_reconnect()
        return handle

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

    handle = VnpyRuntimeHandle(
        settings=settings,
        event_engine=event_engine,
        main_engine=main_engine,
        event_bridge=event_bridge,
        diagnostics=diagnostics,
        event_sink=event_sink,
    )
    handle.start_auto_reconnect()
    return handle


def _add_gateway(
    *,
    main_engine: Any,
    gateway_class_path: str,
    gateway_name: Optional[str],
    diagnostics: Dict[str, Any],
) -> None:
    try:
        gateway_cls = _import_object(gateway_class_path)
    except Exception as exc:  # noqa: BLE001 - optional gateway must not block startup.
        diagnostics["gateway"] = {
            "added": False,
            "reason": "gateway_import_failed",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        logger.warning("vn.py gateway import failed: %s", exc)
        return
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
        try:
            add_gateway(gateway_cls)
        except Exception as exc:  # noqa: BLE001 - optional gateway must not block startup.
            diagnostics["gateway"] = {
                "added": False,
                "reason": "add_gateway_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            logger.warning("vn.py add gateway failed: %s", exc)
            return
    except Exception as exc:  # noqa: BLE001 - optional gateway must not block startup.
        diagnostics["gateway"] = {
            "added": False,
            "reason": "add_gateway_failed",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        logger.warning("vn.py add gateway failed: %s", exc)
        return
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
            "request_accepted": False,
            "connected": False,
            "status": "failed",
            "reason": "connect_unavailable",
        }
        return
    if not gateway_name:
        diagnostics["connect"] = {
            "attempted": True,
            "request_accepted": False,
            "connected": False,
            "status": "failed",
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
                "request_accepted": False,
                "connected": False,
                "status": "failed",
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
                "request_accepted": False,
                "connected": False,
                "status": "failed",
                "reason": "connect_settings_path_required",
            }
            return
        payload = {}
    if not isinstance(payload, dict):
        diagnostics["connect"] = {
            "attempted": True,
            "request_accepted": False,
            "connected": False,
            "status": "failed",
            "reason": "connect_settings_must_be_object",
        }
        return
    diagnostics["connect"] = {
        "attempted": True,
        "request_accepted": False,
        "connected": False,
        "status": "connect_requested",
        "reason": "connection_unconfirmed",
        "settings_path": str(path) if path is not None else None,
        "settings_source": "file" if path is not None else "gateway_defaults",
        "gateway_name": gateway_name,
    }
    try:
        connect(payload, gateway_name)
    except Exception as exc:  # noqa: BLE001 - optional runtime must not block API startup.
        diagnostics["connect"].update(
            {
                "request_accepted": False,
                "connected": False,
                "status": "failed",
                "reason": "connect_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        logger.warning("vn.py gateway connect failed: %s", exc)
        return
    diagnostics["connect"]["request_accepted"] = True
    diagnostics["connect"]["request_accepted_at"] = _utc_iso()
    _refresh_gateway_connection(
        main_engine=main_engine,
        gateway_name=gateway_name,
        diagnostics=diagnostics,
    )


def _refresh_gateway_connection(
    *,
    main_engine: Any,
    gateway_name: Optional[str],
    diagnostics: Dict[str, Any],
) -> None:
    connect_diagnostics = diagnostics.get("connect")
    if (
        not isinstance(connect_diagnostics, dict)
        or not connect_diagnostics.get("attempted")
        or not connect_diagnostics.get("request_accepted")
        or main_engine is None
        or not gateway_name
    ):
        return
    get_gateway = getattr(main_engine, "get_gateway", None)
    gateway = get_gateway(gateway_name) if callable(get_gateway) else None
    if gateway is None:
        connect_diagnostics.update(
            {
                "connected": False,
                "status": "disconnected",
                "reason": "gateway_instance_unavailable",
                "confirmation_source": "main_engine.get_gateway",
            }
        )
        return

    confirmed, source = _gateway_connection_confirmation(gateway)
    connect_diagnostics["confirmation_source"] = source
    if confirmed is True:
        connect_diagnostics.update(
            {
                "connected": True,
                "status": "connected",
                "reason": None,
                "confirmed_at": _utc_iso(),
            }
        )
    elif confirmed is False:
        connect_diagnostics.update(
            {
                "connected": False,
                "status": "disconnected",
                "reason": "gateway_reported_disconnected",
            }
        )
    else:
        connect_diagnostics.update(
            {
                "connected": False,
                "status": "connect_requested",
                "reason": "connection_unconfirmed",
            }
        )


def _gateway_connection_confirmation(gateway: Any) -> Tuple[Optional[bool], str]:
    for method_name in ("get_connection_status", "get_state_snapshot"):
        method = getattr(gateway, method_name, None)
        if not callable(method):
            continue
        try:
            state = method()
        except Exception as exc:  # noqa: BLE001 - diagnostics must stay read-only and resilient.
            logger.warning("vn.py gateway status hook %s failed: %s", method_name, exc)
            return None, f"{method_name}_failed"
        if isinstance(state, bool):
            return state, method_name
        if isinstance(state, dict) and isinstance(state.get("connected"), bool):
            return state["connected"], method_name

    connected = getattr(gateway, "connected", None)
    if isinstance(connected, bool):
        return connected, "gateway.connected"
    return None, "unavailable"


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


def _env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = os.getenv(name)
    try:
        parsed = int(str(value).strip()) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _auto_reconnect_max_interval(settings: VnpyRuntimeSettings) -> float:
    return max(
        settings.auto_reconnect_interval_seconds,
        settings.auto_reconnect_max_interval_seconds,
    )


def _reset_auto_reconnect_backoff(
    state: Dict[str, Any],
    settings: VnpyRuntimeSettings,
) -> None:
    previous_failures = int(state.get("consecutive_failure_count") or 0)
    state["consecutive_failure_count"] = 0
    state["current_interval_seconds"] = settings.auto_reconnect_interval_seconds
    if previous_failures:
        state["backoff_reset_at"] = _utc_iso()


def _increase_auto_reconnect_backoff(
    state: Dict[str, Any],
    settings: VnpyRuntimeSettings,
) -> None:
    failures = int(state.get("consecutive_failure_count") or 0) + 1
    maximum = _auto_reconnect_max_interval(settings)
    multiplier = 2 ** min(failures, 30)
    state["consecutive_failure_count"] = failures
    state["current_interval_seconds"] = min(
        maximum,
        settings.auto_reconnect_interval_seconds * multiplier,
    )


def _within_confirmation_grace(value: Any, grace_seconds: int) -> bool:
    if not value:
        return False
    try:
        accepted_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if accepted_at.tzinfo is None:
        accepted_at = accepted_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - accepted_at.astimezone(timezone.utc)).total_seconds()
    return 0 <= age < grace_seconds
