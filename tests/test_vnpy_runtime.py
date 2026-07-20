# -*- coding: utf-8 -*-
"""Tests for optional vn.py runtime bootstrap."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
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
                "VNPY_PRODUCTION_PREFLIGHT_ENABLED": "",
                "VNPY_AUTO_ATTACH_EVENTS": "",
                "VNPY_AUTO_RECONNECT_ENABLED": "",
                "VNPY_AUTO_RECONNECT_INTERVAL_SECONDS": "",
                "VNPY_AUTO_RECONNECT_MAX_INTERVAL_SECONDS": "",
                "VNPY_AUTO_RECONNECT_CONFIRMATION_GRACE_SECONDS": "",
            },
            clear=False,
        ):
            settings = load_vnpy_runtime_settings()

        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.gateway_class)
        self.assertIsNone(settings.gateway_name)
        self.assertFalse(settings.connect_on_start)
        self.assertFalse(settings.production_preflight_enabled)
        self.assertTrue(settings.auto_attach_events)
        self.assertFalse(settings.auto_reconnect_enabled)
        self.assertEqual(settings.auto_reconnect_interval_seconds, 60)
        self.assertEqual(settings.auto_reconnect_max_interval_seconds, 300)
        self.assertEqual(settings.auto_reconnect_confirmation_grace_seconds, 30)

    def test_load_settings_clamps_auto_reconnect_interval(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VNPY_AUTO_RECONNECT_ENABLED": "true",
                "VNPY_AUTO_RECONNECT_INTERVAL_SECONDS": "1",
                "VNPY_AUTO_RECONNECT_MAX_INTERVAL_SECONDS": "9999",
                "VNPY_AUTO_RECONNECT_CONFIRMATION_GRACE_SECONDS": "9999",
            },
            clear=False,
        ):
            settings = load_vnpy_runtime_settings()

        self.assertTrue(settings.auto_reconnect_enabled)
        self.assertEqual(settings.auto_reconnect_interval_seconds, 5)
        self.assertEqual(settings.auto_reconnect_max_interval_seconds, 3600)
        self.assertEqual(settings.auto_reconnect_confirmation_grace_seconds, 600)

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
            self.assertTrue(handle.diagnostics["connect"]["request_accepted"])
            self.assertEqual(
                handle.diagnostics["connect"]["confirmation_source"],
                "get_state_snapshot",
            )
            self.assertEqual(handle.diagnostics["event_bridge"]["registered_count"], 4)
            self.assertEqual(handle.main_engine.gateways[0][1], "SIM")
            self.assertEqual(handle.main_engine.connects[0][0], {"userid": "paper"})
            self.assertEqual(handle.main_engine.connects[0][1], "SIM")

            account_handler = handle.event_engine.handlers["eAccount."][0]
            account_handler({
                "accountid": "SIM-ACCOUNT",
                "balance": 100000.0,
                "available": 90000.0,
            })
            refreshed = handle.refresh_diagnostics()
            self.assertEqual(
                refreshed["event_bridge"]["observations"]["account"]["count"],
                1,
            )
            self.assertEqual(
                refreshed["event_bridge"]["observations"]["account"]["handled_count"],
                1,
            )

            handle.close()
            self.assertTrue(handle.main_engine.closed)
            self.assertTrue(handle.event_engine.stopped)
            self.assertTrue(
                all(not handlers for handlers in handle.event_engine.handlers.values())
            )

    def test_production_preflight_blocks_builtin_before_connect_and_reconnect(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SimGateway",
                    gateway_name="DSA_SIM",
                    connect_on_start=True,
                    production_preflight_enabled=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                )
            )
        finally:
            _restore_modules(installed)

        manual_result = handle.run_manual_reconnect()
        self.assertEqual(handle.main_engine.connects, [])
        self.assertFalse(handle.diagnostics["connect"]["attempted"])
        self.assertEqual(
            handle.diagnostics["connect"]["reason"],
            "production_preflight_failed",
        )
        self.assertEqual(
            handle.diagnostics["production_preflight"]["failures"],
            ["builtin_gateway_not_external"],
        )
        self.assertFalse(handle.diagnostics["auto_reconnect"]["running"])
        self.assertEqual(
            handle.diagnostics["auto_reconnect"]["reason"],
            "production_preflight_failed",
        )
        self.assertEqual(manual_result["last_result"], "failed")
        self.assertEqual(
            handle.diagnostics["connect"]["preflight_failures"],
            ["builtin_gateway_not_external"],
        )
        self.assertEqual(handle.main_engine.connects, [])
        handle.close()

    def test_production_preflight_rejects_repository_settings_and_missing_keys(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "broker.json"
            settings_path.write_text(json.dumps({"userid": "paper"}), encoding="utf-8")
            try:
                with patch("src.services.vnpy_runtime.REPOSITORY_ROOT", root):
                    handle = bootstrap_vnpy_runtime(
                        settings=VnpyRuntimeSettings(
                            enabled=True,
                            gateway_class="fake_vnpy_gateway:StrictGateway",
                            gateway_name="STRICT",
                            connect_settings_path=str(settings_path),
                            connect_on_start=True,
                            production_preflight_enabled=True,
                            auto_attach_events=False,
                        )
                    )
            finally:
                _restore_modules(installed)

        self.assertEqual(handle.main_engine.connects, [])
        self.assertEqual(
            handle.diagnostics["production_preflight"]["failures"],
            [
                "connect_settings_inside_repository",
                "default_setting_keys_missing",
            ],
        )
        self.assertEqual(
            handle.diagnostics["production_preflight"]["missing_default_keys"],
            ["password"],
        )
        self.assertNotIn(str(settings_path), json.dumps(handle.diagnostics))
        handle.close()

    def test_production_preflight_reports_unregistered_gateway_before_settings(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="missing_gateway:Gateway",
                    gateway_name="MISSING",
                    connect_on_start=True,
                    production_preflight_enabled=True,
                    auto_attach_events=False,
                )
            )
        finally:
            _restore_modules(installed)

        self.assertEqual(handle.main_engine.connects, [])
        self.assertEqual(
            handle.diagnostics["production_preflight"]["failures"],
            ["gateway_not_registered"],
        )
        self.assertEqual(
            handle.diagnostics["connect"]["preflight_failures"],
            ["gateway_not_registered"],
        )
        handle.close()

    def test_production_preflight_allows_complete_external_gateway_settings(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "broker.json"
            settings_path.write_text(
                json.dumps({"userid": "paper", "password": "secret"}),
                encoding="utf-8",
            )
            try:
                handle = bootstrap_vnpy_runtime(
                    settings=VnpyRuntimeSettings(
                        enabled=True,
                        gateway_class="fake_vnpy_gateway:StrictGateway",
                        gateway_name="STRICT",
                        connect_settings_path=str(settings_path),
                        connect_on_start=True,
                        production_preflight_enabled=True,
                        auto_attach_events=False,
                    )
                )
            finally:
                _restore_modules(installed)

        self.assertTrue(handle.diagnostics["production_preflight"]["ok"])
        self.assertEqual(len(handle.main_engine.connects), 1)
        self.assertNotIn("secret", json.dumps(handle.diagnostics))
        self.assertNotIn(str(settings_path), json.dumps(handle.diagnostics))
        handle.close()

    def test_production_preflight_sanitizes_settings_read_failure(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "secret-broker-account.json"
            try:
                handle = bootstrap_vnpy_runtime(
                    settings=VnpyRuntimeSettings(
                        enabled=True,
                        gateway_class="fake_vnpy_gateway:StrictGateway",
                        gateway_name="STRICT",
                        connect_settings_path=str(secret_path),
                        connect_on_start=True,
                        production_preflight_enabled=True,
                        auto_attach_events=False,
                    )
                )
            finally:
                _restore_modules(installed)

            encoded = json.dumps(handle.diagnostics)
            self.assertEqual(handle.main_engine.connects, [])
            self.assertEqual(
                handle.diagnostics["connect"]["reason"],
                "production_preflight_failed",
            )
            self.assertNotIn(str(secret_path), encoded)
            self.assertNotIn("secret-broker-account", encoded)
            handle.close()

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

    def test_bootstrap_keeps_unconfirmed_async_connection_distinct(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:UnknownGateway",
                    gateway_name="UNKNOWN",
                    connect_on_start=True,
                    auto_attach_events=False,
                )
            )
        finally:
            _restore_modules(installed)

        connect = handle.diagnostics["connect"]
        self.assertTrue(connect["request_accepted"])
        self.assertFalse(connect["connected"])
        self.assertEqual(connect["status"], "connect_requested")
        self.assertEqual(connect["reason"], "connection_unconfirmed")
        self.assertEqual(connect["confirmation_source"], "unavailable")
        handle.close()

    def test_bootstrap_connect_failure_degrades_without_raising(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:FailingGateway",
                    gateway_name="FAIL",
                    connect_on_start=True,
                    auto_attach_events=False,
                )
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(handle.diagnostics["available"])
        connect = handle.diagnostics["connect"]
        self.assertFalse(connect["request_accepted"])
        self.assertFalse(connect["connected"])
        self.assertEqual(connect["status"], "failed")
        self.assertEqual(connect["reason"], "connect_failed")
        self.assertEqual(connect["error_type"], "RuntimeError")
        handle.close()

    def test_bootstrap_gateway_import_failure_degrades_without_raising(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="missing_gateway_module:MissingGateway",
                    gateway_name="MISSING",
                    connect_on_start=False,
                    auto_attach_events=False,
                )
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(handle.diagnostics["available"])
        self.assertFalse(handle.diagnostics["gateway"]["added"])
        self.assertEqual(
            handle.diagnostics["gateway"]["reason"],
            "gateway_import_failed",
        )
        self.assertEqual(
            handle.diagnostics["gateway"]["error_type"],
            "ModuleNotFoundError",
        )
        handle.close()

    def test_refresh_diagnostics_observes_gateway_disconnect(self) -> None:
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
            gateway = handle.main_engine.get_gateway("SIM")
            gateway.connected = False
            refreshed = handle.refresh_diagnostics()
        finally:
            _restore_modules(installed)

        self.assertFalse(refreshed["connect"]["connected"])
        self.assertEqual(refreshed["connect"]["status"], "disconnected")
        self.assertEqual(
            refreshed["connect"]["reason"],
            "gateway_reported_disconnected",
        )
        handle.close()

    def test_auto_reconnect_check_recovers_confirmed_disconnect(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SimGateway",
                    gateway_name="SIM",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                )
            )
            gateway = handle.main_engine.get_gateway("SIM")
            gateway.connected = False
            result = handle.run_auto_reconnect_check()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["last_result"], "reconnected")
        self.assertTrue(handle.diagnostics["connect"]["connected"])
        self.assertEqual(len(handle.main_engine.connects), 2)

    def test_manual_reconnect_works_when_auto_monitor_is_disabled(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SimGateway",
                    gateway_name="SIM",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=False,
                )
            )
            gateway = handle.main_engine.get_gateway("SIM")
            gateway.connected = False
            result = handle.run_manual_reconnect()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(result["last_trigger"], "manual")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["last_result"], "reconnected")
        self.assertTrue(handle.diagnostics["connect"]["connected"])

    def test_manual_reconnect_can_start_configured_gateway_on_demand(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SimGateway",
                    gateway_name="SIM",
                    connect_on_start=False,
                    auto_attach_events=False,
                    auto_reconnect_enabled=False,
                )
            )
            result = handle.run_manual_reconnect()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["last_result"], "reconnected")
        self.assertEqual(len(handle.main_engine.connects), 1)

    def test_manual_reconnect_respects_async_confirmation_grace(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SlowGateway",
                    gateway_name="SLOW",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=False,
                    auto_reconnect_confirmation_grace_seconds=30,
                )
            )
            result = handle.run_manual_reconnect()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(result["last_check_result"], "confirmation_pending")
        self.assertEqual(len(handle.main_engine.connects), 1)

    def test_auto_reconnect_monitor_recovers_without_status_reads(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SimGateway",
                    gateway_name="SIM",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=0.05,
                )
            )
            gateway = handle.main_engine.get_gateway("SIM")
            gateway.connected = False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = handle.diagnostics["auto_reconnect"]
                if state.get("success_count") == 1:
                    break
                time.sleep(0.02)
            state = dict(handle.diagnostics["auto_reconnect"])
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(state["attempt_count"], 1)
        self.assertEqual(state["success_count"], 1)
        self.assertEqual(state["failure_count"], 0)
        self.assertEqual(state["last_result"], "reconnected")
        self.assertTrue(handle.diagnostics["connect"]["connected"])

    def test_auto_reconnect_check_does_not_retry_unconfirmed_gateway(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:UnknownGateway",
                    gateway_name="UNKNOWN",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                )
            )
            result = handle.run_auto_reconnect_check()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(result["last_check_result"], "not_required")
        self.assertEqual(result["last_check_reason"], "connect_requested")
        self.assertEqual(len(handle.main_engine.connects), 1)

    def test_auto_reconnect_check_audits_failed_retry(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:FailingGateway",
                    gateway_name="FAIL",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                )
            )
            result = handle.run_auto_reconnect_check()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["consecutive_failure_count"], 1)
        self.assertEqual(result["current_interval_seconds"], 10)
        self.assertEqual(result["last_result"], "failed")
        self.assertEqual(result["last_reason"], "connect_failed")
        self.assertEqual(len(handle.main_engine.connects), 2)

    def test_auto_reconnect_backoff_caps_and_resets_after_recovery(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:RecoveringGateway",
                    gateway_name="RECOVERING",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                    auto_reconnect_max_interval_seconds=8,
                )
            )
            first = dict(handle.run_auto_reconnect_check())
            second = dict(handle.run_auto_reconnect_check())
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(first["consecutive_failure_count"], 1)
        self.assertEqual(first["current_interval_seconds"], 8)
        self.assertEqual(second["success_count"], 1)
        self.assertEqual(second["consecutive_failure_count"], 0)
        self.assertEqual(second["current_interval_seconds"], 5)
        self.assertIsNotNone(second["backoff_reset_at"])

    def test_reconnect_events_are_deduplicated_and_drained_on_close(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        events = []
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:RecoveringGateway",
                    gateway_name="RECOVERING",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                    auto_reconnect_max_interval_seconds=300,
                ),
                event_sink=lambda **event: events.append(event),
            )
            handle.run_auto_reconnect_check()
            handle.run_auto_reconnect_check()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "vnpy_gateway_reconnect_failed",
                "vnpy_gateway_reconnected",
            ],
        )
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[1]["status"], "resolved")
        self.assertEqual(events[0]["diagnostics"]["gateway_name"], "RECOVERING")
        self.assertNotIn("settings_path", events[0]["diagnostics"])

    def test_repeated_reconnect_failures_emit_one_open_event(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        events = []
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:FailingGateway",
                    gateway_name="FAIL",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                    auto_reconnect_max_interval_seconds=300,
                ),
                event_sink=lambda **event: events.append(event),
            )
            handle.run_auto_reconnect_check()
            handle.run_auto_reconnect_check()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(
            [event["event_type"] for event in events],
            ["vnpy_gateway_reconnect_failed"],
        )

    def test_reconnect_backoff_cap_emits_degraded_event(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        events = []
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:FailingGateway",
                    gateway_name="FAIL",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                    auto_reconnect_max_interval_seconds=8,
                ),
                event_sink=lambda **event: events.append(event),
            )
            handle.run_auto_reconnect_check()
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "vnpy_gateway_reconnect_failed",
                "vnpy_gateway_reconnect_backoff_capped",
            ],
        )
        capped = events[1]
        self.assertEqual(capped["status"], "degraded")
        self.assertEqual(capped["observed_value"], 8)
        self.assertEqual(capped["threshold"], 8)
        persisted_diagnostics = {
            "event_type": capped["event_type"],
            "source": "vnpy_paper_auto",
            **capped["diagnostics"],
        }
        encoded_diagnostics = json.dumps(
            persisted_diagnostics,
            ensure_ascii=False,
        )
        self.assertLessEqual(len(encoded_diagnostics), 300)
        self.assertEqual(json.loads(encoded_diagnostics), persisted_diagnostics)

    def test_auto_reconnect_waits_for_async_connection_confirmation(self) -> None:
        installed = _install_fake_vnpy_runtime_modules()
        try:
            handle = bootstrap_vnpy_runtime(
                settings=VnpyRuntimeSettings(
                    enabled=True,
                    gateway_class="fake_vnpy_gateway:SlowGateway",
                    gateway_name="SLOW",
                    connect_on_start=True,
                    auto_attach_events=False,
                    auto_reconnect_enabled=True,
                    auto_reconnect_interval_seconds=5,
                    auto_reconnect_confirmation_grace_seconds=30,
                )
            )
            pending = dict(handle.run_auto_reconnect_check())
            handle.diagnostics["connect"]["request_accepted_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=31)
            ).isoformat()
            retried = dict(handle.run_auto_reconnect_check())
        finally:
            handle.close()
            _restore_modules(installed)

        self.assertEqual(pending["attempt_count"], 0)
        self.assertEqual(pending["last_check_result"], "confirmation_pending")
        self.assertEqual(
            pending["last_check_reason"],
            "connection_confirmation_grace",
        )
        self.assertEqual(retried["attempt_count"], 1)
        self.assertEqual(retried["failure_count"], 1)
        self.assertEqual(len(handle.main_engine.connects), 2)

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
        self.assertFalse(handle.diagnostics["connect"]["request_accepted"])
        self.assertEqual(handle.diagnostics["connect"]["status"], "failed")
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
            self.gateway_instances[gateway_name] = gateway_class()

        def get_gateway(self, gateway_name):
            return self.gateway_instances.get(gateway_name)

        def connect(self, setting, gateway_name):
            self.connects.append((setting, gateway_name))
            gateway = self.gateway_instances[gateway_name]
            gateway.connect(setting)

        def close(self):
            self.closed = True

    class SimGateway:
        connect_without_settings = True

        def __init__(self):
            self.connected = False

        def connect(self, setting):
            self.connected = True

        def get_state_snapshot(self):
            return {"connected": self.connected}

    class StrictGateway:
        connect_without_settings = False
        default_setting = {"userid": "", "password": ""}

        def connect(self, setting):
            return None

    class UnknownGateway:
        connect_without_settings = True

        def connect(self, setting):
            return None

    class FailingGateway:
        connect_without_settings = True

        def connect(self, setting):
            raise RuntimeError("paper gateway login rejected")

    class SlowGateway:
        connect_without_settings = True

        def __init__(self):
            self.connected = False

        def connect(self, setting):
            return None

    class RecoveringGateway:
        connect_without_settings = True

        def __init__(self):
            self.connected = False
            self.failures_remaining = 2

        def connect(self, setting):
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise RuntimeError("temporary login failure")
            self.connected = True

    event_module.EventEngine = EventEngine
    engine_module.MainEngine = MainEngine
    trader_event_module.EVENT_ORDER = "eOrder."
    trader_event_module.EVENT_TRADE = "eTrade."
    trader_event_module.EVENT_ACCOUNT = "eAccount."
    trader_event_module.EVENT_POSITION = "ePosition."
    gateway_module.SimGateway = SimGateway
    gateway_module.StrictGateway = StrictGateway
    gateway_module.UnknownGateway = UnknownGateway
    gateway_module.FailingGateway = FailingGateway
    gateway_module.SlowGateway = SlowGateway
    gateway_module.RecoveringGateway = RecoveringGateway
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
