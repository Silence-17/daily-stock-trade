from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.services.vnpy_paper_trading_service import _last_auto_trade_ran_in_session


def _window():
    return {
        "is_trading_day": True,
        "session_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    }


def test_daily_schedule_ignores_persisted_dry_run():
    service = MagicMock()
    service._read_config_payload.return_value = {
        "last_auto_run": {
            "market": "cn",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "execution_mode": "dry_run",
        }
    }

    assert not _last_auto_trade_ran_in_session(
        service,
        SimpleNamespace(auto_market="cn"),
        _window(),
    )
    service.agent_repo.get_run_detail.assert_not_called()


def test_daily_schedule_resolves_legacy_run_mode_from_agent_audit():
    service = MagicMock()
    service._read_config_payload.return_value = {
        "last_auto_run": {
            "market": "cn",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "agent_run_uid": "legacy-dry-run",
        }
    }
    service.agent_repo.get_run_detail.return_value = {
        "diagnostics": {"execution_mode": "manual_approval"}
    }

    assert not _last_auto_trade_ran_in_session(
        service,
        SimpleNamespace(auto_market="cn"),
        _window(),
    )
