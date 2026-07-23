# -*- coding: utf-8 -*-
"""Security and behavior contract for credentialed CTP acceptance."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vnpy-ctp-account-soak.yml"


def test_ctp_account_soak_is_manual_protected_and_serialized() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["ctp-account-soak"]
    assert job["environment"] == "vnpy-ctp-paper-acceptance"
    assert job["runs-on"] == "windows-2022"
    assert "VNPY_CTP_CONNECT_SETTINGS_JSON" not in job["env"]
    materialize = next(
        step
        for step in job["steps"]
        if step.get("name") == "Materialize protected connection settings"
    )
    assert materialize["env"] == {
        "VNPY_CTP_CONNECT_SETTINGS_JSON": "${{ secrets.VNPY_CTP_CONNECT_SETTINGS_JSON }}"
    }
    steps = job["steps"]
    materialize_index = steps.index(materialize)
    install_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install application and CTP runtime"
    )
    assert materialize_index < install_index
    assert "secrets.VNPY_CTP_CONNECT_SETTINGS_JSON" in text
    assert text.count("secrets.VNPY_CTP_CONNECT_SETTINGS_JSON") == 1
    assert "vnpy_ctp==6.7.11.4" in text
    assert "VNPY_PRODUCTION_PREFLIGHT_ENABLED: 'true'" in text
    assert "GITHUB_REF -ne 'refs/heads/main'" in text


def test_ctp_account_soak_validates_exact_secret_schema_before_install() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["ctp-account-soak"]["steps"]
    materialize = next(
        step
        for step in steps
        if step.get("name") == "Materialize protected connection settings"
    )
    script = materialize["run"]

    for key in (
        "用户名",
        "密码",
        "经纪商代码",
        "交易服务器",
        "行情服务器",
        "产品名称",
        "授权编码",
        "柜台环境",
    ):
        assert f"'{key}'" in script
    assert "$missingKeys" in script
    assert "$unexpectedKeys" in script
    assert "$invalidValueKeys" in script
    assert "-match '[\\r\\n]'" in script
    assert "CTP settings schema mismatch" in script
    assert script.index("CTP settings schema mismatch") < script.index("::add-mask::")
    assert "Write-Output $raw" not in script


def test_ctp_account_soak_cleanup_uses_the_fixed_temp_path_as_fallback() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    cleanup = next(
        step
        for step in workflow["jobs"]["ctp-account-soak"]["steps"]
        if step.get("name") == "Remove connection settings"
    )
    script = cleanup["run"]

    assert cleanup["if"] == "always()"
    assert "IsNullOrWhiteSpace($env:VNPY_CONNECT_SETTINGS_PATH)" in script
    assert "Join-Path $runnerRoot 'vnpy-ctp-connect.json'" in script
    assert "Refusing to remove a settings file outside RUNNER_TEMP" in script
    assert "Remove-Item -LiteralPath $settingsPath" in script


def test_ctp_account_soak_is_external_no_order_and_sanitized() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "--require-external-gateway" in text
    assert "--require-all-default-keys" in text
    assert "--require-event', 'account" in text
    assert "tests/test_vnpy_ctp_runtime.py" in text
    assert "actions/upload-artifact@v6" in text
    assert "retention-days: 14" in text
    assert "RUNNER_TEMP" in text
    assert "Remove-Item -LiteralPath $settingsPath" in text
    assert "send_order" not in text
    assert "cancel_order" not in text
    assert "--require-event', 'order" not in text
    assert "--require-event', 'trade" not in text
