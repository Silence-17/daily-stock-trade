# -*- coding: utf-8 -*-
"""Tests for the explicit vn.py gateway plugin manifest."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from scripts.vnpy_gateway_plugins import (
    GatewayPlugin,
    install_gateway_plugins,
    main,
    parse_gateway_plugins,
    verify_gateway_plugins,
)


def test_parse_gateway_plugins_accepts_exact_registry_requirements() -> None:
    plugins = parse_gateway_plugins(json.dumps([
        {"package": "sample-gateway[fast]==1.2.3", "module": "sample_gateway"},
        {"package": "another-gateway===2.0+vendor", "module": "another.gateway"},
    ]))

    assert plugins == [
        GatewayPlugin("sample-gateway[fast]==1.2.3", "sample_gateway"),
        GatewayPlugin("another-gateway===2.0+vendor", "another.gateway"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '[{"package":"-e bad","module":"bad"}]',
        '[{"package":"pkg @ https://example.invalid/pkg.whl","module":"pkg"}]',
        '[{"package":"pkg; python_version > \'3\'","module":"pkg"}]',
        '[{"package":"pkg","module":"pkg"}]',
        '[{"package":"pkg>=1,<2","module":"pkg"}]',
        '[{"package":"pkg==1.*","module":"pkg"}]',
        '[{"package":"pkg","module":"pkg:Gateway"}]',
        '[{"package":"pkg","module":"pkg","extra":true}]',
        '[{"package":"pkg","module":"pkg"},{"package":"PKG","module":"other"}]',
        '[{"package":"one","module":"same"},{"package":"two","module":"same"}]',
    ],
)
def test_parse_gateway_plugins_rejects_unsafe_or_ambiguous_input(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_gateway_plugins(payload)


def test_install_gateway_plugins_passes_each_requirement_as_one_argument() -> None:
    plugins = [GatewayPlugin("sample-gateway==1.2.3", "sample_gateway")]

    with patch("scripts.vnpy_gateway_plugins.subprocess.run") as run:
        install_gateway_plugins(plugins)

    command = run.call_args.args[0]
    assert command[-1] == "sample-gateway==1.2.3"
    assert command[-2] == "https://pypi.vnpy.com"
    run.assert_called_once_with(command, check=True)


def test_install_gateway_plugins_propagates_pip_failure() -> None:
    with patch(
        "scripts.vnpy_gateway_plugins.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["pip"]),
    ), pytest.raises(subprocess.CalledProcessError):
        install_gateway_plugins([GatewayPlugin("sample-gateway", "sample_gateway")])


def test_verify_gateway_plugins_imports_declared_module() -> None:
    plugins = [GatewayPlugin("sample-gateway", "sample_gateway")]

    with patch("scripts.vnpy_gateway_plugins.importlib.import_module") as import_module:
        verify_gateway_plugins(plugins)

    import_module.assert_called_once_with("sample_gateway")


def test_cli_prints_module_array(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([
        "--manifest-json",
        '[{"package":"packaging==26.2","module":"packaging"}]',
        "--verify",
        "--print-modules-json",
    ]) == 0

    assert json.loads(capsys.readouterr().out) == ["packaging"]


def test_cli_rejects_plugins_when_vnpy_profile_is_disabled() -> None:
    with pytest.raises(SystemExit) as raised:
        main([
            "--manifest-json",
            '[{"package":"packaging==26.2","module":"packaging"}]',
            "--assert-empty",
        ])

    assert raised.value.code == 2
