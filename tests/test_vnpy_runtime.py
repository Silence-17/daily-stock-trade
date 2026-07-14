# -*- coding: utf-8 -*-
"""Tests for optional vn.py runtime bootstrap."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.vnpy_runtime import (
    VnpyRuntimeSettings,
    bootstrap_vnpy_runtime,
    load_vnpy_runtime_settings,
)
from src.config import Config


class VnpyRuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._environ = dict(os.environ)
        Config.reset_instance()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._environ)
        Config.reset_instance()

    def test_load_settings_defaults_to_disabled(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VNPY_RUNTIME_ENABLED": "",
                "VNPY_GATEWAY_CLASS": "",
                "VNPY_GATEWAY_NAME": "",
                "VNPY_CONNECT_SETTINGS_PATH": "",
                "VNPY_CONNECT_ON_START": "",
                "VNPY_AUTO_ATTACH_EVENTS": "",
            },
            clear=False,
        ):
            settings = load_vnpy_runtime_settings()

        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.gateway_class)
        self.assertIsNone(settings.gateway_name)
        self.assertFalse(settings.connect_on_start)
        self.assertTrue(settings.auto_attach_events)

    def test_bootstrap_disabled_returns_diagnostics_without_importing_vnpy(self) -> None:
        handle = bootstrap_vnpy_runtime(settings=VnpyRuntimeSettings(enabled=False))

        self.assertFalse(handle.diagnostics["available"])
        self.assertEqual(handle.diagnostics["reason"], "disabled")
        self.assertIsNone(handle.main_engine)
        self.assertIsNone(handle.event_engine)

    def test_bootstrap_enabled_degrades_when_vnpy_import_fails(self) -> None:
        def _missing_import(module_name: str):
            raise ModuleNotFoundError(f"No module named {module_name!r}", name="vnpy")

        with patch("src.services.vnpy_runtime.importlib.import_module", side_effect=_missing_import):
            handle = bootstrap_vnpy_runtime(settings=VnpyRuntimeSettings(enabled=True))

        self.assertFalse(handle.diagnostics["available"])
        self.assertEqual(handle.diagnostics["reason"], "bootstrap_failed")
        self.assertEqual(handle.diagnostics["error_type"], "ModuleNotFoundError")

    def test_bootstrap_prepares_local_vnpy_runtime_directory(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            try:
                with patch("src.services.vnpy_runtime.Path.cwd", return_value=runtime_root):
                    handle = bootstrap_vnpy_runtime(
                        settings=VnpyRuntimeSettings(
                            enabled=True,
                            auto_attach_events=False,
                        )
                    )
            finally:
                _restore_modules(installed)

            self.assertTrue(runtime_root.joinpath(".vntrader").is_dir())
            self.assertEqual(
                handle.diagnostics["runtime_data_dir"],
                str(runtime_root.joinpath(".vntrader")),
            )
            handle.close()

    def test_bootstrap_adds_gateway_connects_and_attaches_events(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            connect_path = tmp_path / "vnpy-connect.json"
            connect_path.write_text(json.dumps({"userid": "paper"}), encoding="utf-8")
            try:
                handle = bootstrap_vnpy_runtime(
                    settings=VnpyRuntimeSettings(
                        enabled=True,
                        gateway_class="fake_vnpy_gateway:SimGateway",
                        gateway_name="SIM",
                        connect_settings_path=str(connect_path),
                        connect_on_start=True,
                        auto_attach_events=True,
                    ),
                    config_path=tmp_path / "vnpy-paper.json",
                )
            finally:
                _restore_modules(installed)

            self.assertTrue(handle.diagnostics["available"])
            self.assertTrue(handle.diagnostics["gateway"]["added"])
            self.assertTrue(handle.diagnostics["connect"]["connected"])
            self.assertEqual(handle.diagnostics["event_bridge"]["registered_count"], 4)
            self.assertEqual(handle.main_engine.gateways[0][1], "SIM")
            self.assertEqual(handle.main_engine.connects[0][0], {"userid": "paper"})
            self.assertEqual(handle.main_engine.connects[0][1], "SIM")

            handle.close()
            self.assertTrue(handle.main_engine.closed)
            self.assertTrue(handle.event_engine.stopped)
            self.assertTrue(all(not handlers for handlers in handle.event_engine.handlers.values()))

    def test_bootstrap_connects_opt_in_gateway_without_settings_file(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SimGateway",
                    gateway_name="SIM",
                    connect_on_start=True,
                    auto_attach_events=False,
                )
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(handle.diagnostics["connect"]["connected"])
        self.assertEqual(handle.diagnostics["connect"]["settings_source"], "gateway_defaults")
        self.assertIsNone(handle.diagnostics["connect"]["settings_path"])
        self.assertEqual(handle.main_engine.connects[0], ({}, "SIM"))
        handle.close()

    def test_bootstrap_keeps_settings_file_required_for_regular_gateway(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:StrictGateway",
                    gateway_name="STRICT",
                    connect_on_start=True,
                    auto_attach_events=False,
                )
            )
        finally:
            _restore_modules(installed)

        self.assertFalse(handle.diagnostics["connect"]["connected"])
        self.assertEqual(
            handle.diagnostics["connect"]["reason"],
            "connect_settings_path_required",
        )
        self.assertEqual(handle.main_engine.connects, [])
        handle.close()


def _install_fake_vnpy_runtime_modules() -> dict[str, object]:
    names = [
        "vnpy",
        "vnpy.event",
        "vnpy.trader",
        "vnpy.trader.engine",
        "vnpy.trader.event",
        "fake_vnpy_gateway",
    ]
    previous = {name: sys.modules.get(name) for name in names}
    vnpy_module = types.ModuleType("vnpy")
    event_module = types.ModuleType("vnpy.event")
    trader_module = types.ModuleType("vnpy.trader")
    engine_module = types.ModuleType("vnpy.trader.engine")
    trader_event_module = types.ModuleType("vnpy.trader.event")
    gateway_module = types.ModuleType("fake_vnpy_gateway")

    class EventEngine:
        def __init__(self) -> None:
            self.handlers = {}
            self.stopped = False

        def register(self, event_type, handler):
            self.handlers.setdefault(event_type, []).append(handler)

        def unregister(self, event_type, handler):
            handlers = self.handlers.get(event_type, [])
            self.handlers[event_type] = [item for item in handlers if item is not handler]

        def stop(self):
            self.stopped = True

    class MainEngine:
        def __init__(self, event_engine) -> None:
            self.event_engine = event_engine
            self.gateways = []
            self.gateway_instances = {}
            self.connects = []
            self.closed = False

        def add_gateway(self, gateway_class, gateway_name=""):
            self.gateways.append((gateway_class, gateway_name))
            self.gateway_instances[gateway_name] = types.SimpleNamespace(
                connect_without_settings=bool(
                    getattr(gateway_class, "connect_without_settings", False)
                )
            )

        def get_gateway(self, gateway_name):
            return self.gateway_instances.get(gateway_name)

        def connect(self, setting, gateway_name):
            self.connects.append((setting, gateway_name))

        def close(self):
            self.closed = True

    class SimGateway:
        connect_without_settings = True

    class StrictGateway:
        connect_without_settings = False

    event_module.EventEngine = EventEngine
    engine_module.MainEngine = MainEngine
    trader_event_module.EVENT_ORDER = "eOrder."
    trader_event_module.EVENT_TRADE = "eTrade."
    trader_event_module.EVENT_ACCOUNT = "eAccount."
    trader_event_module.EVENT_POSITION = "ePosition."
    gateway_module.SimGateway = SimGateway
    gateway_module.StrictGateway = StrictGateway
    vnpy_module.event = event_module
    vnpy_module.trader = trader_module
    trader_module.engine = engine_module
    trader_module.event = trader_event_module

    sys.modules["vnpy"] = vnpy_module
    sys.modules["vnpy.event"] = event_module
    sys.modules["vnpy.trader"] = trader_module
    sys.modules["vnpy.trader.engine"] = engine_module
    sys.modules["vnpy.trader.event"] = trader_event_module
    sys.modules["fake_vnpy_gateway"] = gateway_module
    return previous


def _restore_modules(previous: dict[str, object]) -> None:
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
