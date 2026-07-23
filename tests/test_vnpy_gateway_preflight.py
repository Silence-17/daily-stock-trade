# -*- coding: utf-8 -*-
"""Tests for the zero-connect vn.py gateway preflight."""

from __future__ import annotations

import json
import logging

from loguru import logger as loguru_logger

from scripts.check_vnpy_gateway_preflight import (
    _close_log_handlers_under,
    _is_builtin_gateway,
    _read_settings_keys,
    evaluate_preflight,
)


def test_preflight_accepts_registered_external_gateway_contract() -> None:
    result = evaluate_preflight(
        runtime_available=True,
        gateway_registered=True,
        gateway_class="vnpy_ctp:CtpGateway",
        gateway_name="CTP",
        settings_required=True,
        settings_provided=True,
        settings_valid=True,
        settings_inside_repository=False,
        missing_default_keys=[],
        empty_default_keys=[],
        require_external_gateway=True,
        require_settings_outside_repository=True,
        require_all_default_keys=True,
    )

    assert result["ok"] is True
    assert result["failures"] == []


def test_preflight_rejects_builtin_and_unsafe_or_incomplete_settings() -> None:
    result = evaluate_preflight(
        runtime_available=True,
        gateway_registered=True,
        gateway_class="src.services.vnpy_simulated_gateway:DsaSimulatedGateway",
        gateway_name="DSA_SIM",
        settings_required=True,
        settings_provided=True,
        settings_valid=True,
        settings_inside_repository=True,
        missing_default_keys=["password", "userid"],
        empty_default_keys=["broker"],
        require_external_gateway=True,
        require_settings_outside_repository=True,
        require_all_default_keys=True,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "builtin_gateway_not_external",
        "connect_settings_inside_repository",
        "default_setting_keys_missing",
        "default_setting_values_empty",
    ]


def test_settings_reader_reports_only_shape_and_error_type(tmp_path) -> None:
    secret = "never-print-this-password"
    path = tmp_path / "broker.json"
    path.write_text(json.dumps({"userid": "paper", "password": secret}), encoding="utf-8")

    payload, diagnostics = _read_settings_keys(path)

    assert payload["password"] == secret
    encoded = json.dumps(diagnostics)
    assert diagnostics["provided"] is True
    assert diagnostics["valid"] is True
    assert secret not in encoded
    assert str(path) not in encoded


def test_settings_reader_rejects_non_object_without_leaking_path(tmp_path) -> None:
    path = tmp_path / "broker.json"
    path.write_text('["secret"]', encoding="utf-8")

    payload, diagnostics = _read_settings_keys(path)

    assert payload == {}
    assert diagnostics["valid"] is False
    assert diagnostics["read_error_type"] == "SettingsMustBeObject"
    assert str(path) not in json.dumps(diagnostics)


def test_builtin_gateway_detection_covers_class_and_name_aliases() -> None:
    assert _is_builtin_gateway(
        "src.services.vnpy_simulated_gateway:DsaSimulatedGateway",
        "CUSTOM",
    )
    assert _is_builtin_gateway("vendor.gateway:Gateway", "DSA_SIM")
    assert not _is_builtin_gateway("vnpy_ctp:CtpGateway", "CTP")


def test_log_cleanup_only_closes_handlers_inside_preflight_directory(tmp_path) -> None:
    inside = tmp_path / "preflight"
    outside = tmp_path / "application"
    inside.mkdir()
    outside.mkdir()
    standard_inside = logging.FileHandler(inside / "stdlib.log", encoding="utf-8")
    standard_outside = logging.FileHandler(outside / "stdlib.log", encoding="utf-8")
    test_logger = logging.getLogger("test_vnpy_gateway_preflight_cleanup")
    test_logger.addHandler(standard_inside)
    test_logger.addHandler(standard_outside)
    loguru_inside = loguru_logger.add(inside / "loguru.log")
    loguru_outside = loguru_logger.add(outside / "loguru.log")

    try:
        _close_log_handlers_under(inside)

        assert standard_inside not in test_logger.handlers
        assert standard_outside in test_logger.handlers
        assert loguru_inside not in loguru_logger._core.handlers
        assert loguru_outside in loguru_logger._core.handlers
    finally:
        for handler in (standard_inside, standard_outside):
            handler.close()
            test_logger.removeHandler(handler)
        for handler_id in (loguru_inside, loguru_outside):
            if handler_id in loguru_logger._core.handlers:
                loguru_logger.remove(handler_id)
