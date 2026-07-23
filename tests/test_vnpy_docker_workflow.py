from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "vnpy-docker-smoke.yml"


def test_vnpy_docker_acceptance_workflow_is_manual_and_fail_closed() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    assert workflow[True] == {"workflow_dispatch": None}
    assert "INCLUDE_VNPY=true" in text
    assert 'VNPY_GATEWAY_PLUGINS_JSON=[{"package":"vnpy==4.4.0","module":"vnpy"}]' in text
    assert "stock-analysis:default-smoke" in text
    assert "stock-analysis:vnpy-smoke" in text
    assert "VNPY_RUNTIME_ENABLED=true" in text
    assert "src.services.vnpy_simulated_gateway:DsaSimulatedGateway" in text
    assert 'runtime["mode"] == "vnpy_runtime"' in text
    assert 'runtime["gateway"]["gateway_name"] == "DSA_SIM"' in text
    assert 'runtime["event_bridge"]["registered_count"] == 4' in text
    assert 'runtime["event_bridge"]["handler_failure_count"] == 0' in text
    assert 'scheduler["loop_running"] is True' in text
    assert 'DELTA_BYTES="$((VNPY_BYTES - DEFAULT_BYTES))"' in text
    assert "if: failure()" in text
    assert "if: always()" in text


def test_vnpy_docker_acceptance_workflow_does_not_publish_or_use_credentials() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "push: true" not in text
    assert "docker/login-action" not in text
    assert "VNPY_CONNECT_SETTINGS_PATH" not in text
    assert "secrets." not in text
