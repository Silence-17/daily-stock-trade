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
    assert "secrets.VNPY_CTP_CONNECT_SETTINGS_JSON" in text
    assert text.count("secrets.VNPY_CTP_CONNECT_SETTINGS_JSON") == 1
    assert "vnpy_ctp==6.7.11.4" in text
    assert "VNPY_PRODUCTION_PREFLIGHT_ENABLED: 'true'" in text


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
