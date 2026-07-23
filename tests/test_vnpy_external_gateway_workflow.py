# -*- coding: utf-8 -*-
"""Contract checks for the credential-free external Gateway workflow."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vnpy-external-gateway-smoke.yml"


def test_external_gateway_workflow_is_manual_zero_connect_ctp_acceptance() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    assert workflow[True] == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert "vnpy_ctp==6.7.11.4" in text
    assert 'gateway_class="vnpy_ctp:CtpGateway"' in text
    assert 'gateway_name="CTP"' in text
    assert "settings_path=None" in text
    assert 'result["evaluation"]["failures"] == ["connect_settings_required"]' in text
    assert '"connect_called": False' in text
    assert '"orders_created": False' in text
    assert "build-backend.ps1" in text
    assert "-IncludeVnpy" in text


def test_external_gateway_workflow_does_not_read_credentials_or_publish() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "secrets." not in text
    assert "VNPY_CONNECT_SETTINGS_PATH" not in text
    assert "docker push" not in text
    assert "actions/upload-artifact" not in text
