"""Regression tests for the Windows forward-paper host task."""

from __future__ import annotations

import re
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class CrossMarketPaperTaskScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "install_cross_market_paper_30d_task.ps1"
        ).read_text(encoding="utf-8")

    def test_all_market_window_wake_triggers_are_persisted(self) -> None:
        trigger_times = re.findall(r'-At "(\d{2}:\d{2})"', self.script)

        self.assertEqual(
            trigger_times,
            [
                "07:45",
                "08:50",
                "09:23",
                "10:38",
                "14:55",
                "21:14",
                "21:25",
                "22:14",
                "22:25",
                "23:25",
                "00:55",
                "01:55",
                "03:55",
                "04:55",
            ],
        )
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", self.script)
        self.assertIn("-WakeToRun", self.script)

    def test_wake_triggers_preserve_exchange_day_groups(self) -> None:
        weekday_prefix = (
            "New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 "
            "-DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At"
        )
        overnight_prefix = (
            "New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 "
            "-DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At"
        )

        for trigger_time in (
            "07:45",
            "08:50",
            "09:23",
            "14:55",
            "21:14",
            "21:25",
            "22:14",
            "22:25",
            "23:25",
        ):
            self.assertIn(f'{weekday_prefix} "{trigger_time}"', self.script)
        for trigger_time in ("00:55", "01:55", "03:55", "04:55"):
            self.assertIn(f'{overnight_prefix} "{trigger_time}"', self.script)

    def test_us_wake_triggers_cover_dst_and_standard_market_windows(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        new_york = ZoneInfo("America/New_York")

        summer_wakes = [
            datetime(2026, 7, 28, 21, 25, tzinfo=shanghai),
            datetime(2026, 7, 28, 22, 25, tzinfo=shanghai),
            datetime(2026, 7, 29, 3, 55, tzinfo=shanghai),
        ]
        winter_wakes = [
            datetime(2026, 12, 1, 22, 25, tzinfo=shanghai),
            datetime(2026, 12, 1, 23, 25, tzinfo=shanghai),
            datetime(2026, 12, 2, 4, 55, tzinfo=shanghai),
        ]
        premarket_wakes = [
            datetime(2026, 7, 28, 21, 14, tzinfo=shanghai),
            datetime(2026, 12, 1, 22, 14, tzinfo=shanghai),
        ]
        early_close_wakes = [
            datetime(2026, 7, 4, 0, 55, tzinfo=shanghai),
            datetime(2026, 11, 28, 1, 55, tzinfo=shanghai),
        ]

        self.assertEqual(
            [item.astimezone(new_york).strftime("%H:%M") for item in summer_wakes],
            ["09:25", "10:25", "15:55"],
        )
        self.assertEqual(
            [item.astimezone(new_york).strftime("%H:%M") for item in winter_wakes],
            ["09:25", "10:25", "15:55"],
        )
        self.assertEqual(
            [item.astimezone(new_york).strftime("%H:%M") for item in premarket_wakes],
            ["09:14", "09:14"],
        )
        self.assertEqual(
            [item.astimezone(new_york).strftime("%H:%M") for item in early_close_wakes],
            ["12:55", "12:55"],
        )

    def test_cn_closing_snapshot_has_a_dedicated_wake_trigger(self) -> None:
        self.assertIn(
            "-DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday "
            '-At "14:55"',
            self.script,
        )

    def test_only_bounded_cn_intraday_recovery_has_a_wake_trigger(self) -> None:
        self.assertIn(
            "-DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday "
            '-At "10:38"',
            self.script,
        )
        for trigger_time in ("13:28", "14:28"):
            self.assertNotIn(
                "-DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday "
                f'-At "{trigger_time}"',
                self.script,
            )

    def test_host_settings_keep_the_forward_campaign_alive(self) -> None:
        for setting in (
            "-ExecutionTimeLimit ([TimeSpan]::Zero)",
            "-MultipleInstances IgnoreNew",
            "-RestartCount 999",
            "-RestartInterval (New-TimeSpan -Minutes 1)",
            "-StartWhenAvailable",
            "-WakeToRun",
        ):
            self.assertIn(setting, self.script)
        self.assertIn(
            'New-ScheduledTaskPrincipal -UserId "SYSTEM" '
            "-LogonType ServiceAccount -RunLevel Highest",
            self.script,
        )

    def test_installer_prepares_power_plan_for_reliable_wake(self) -> None:
        self.assertIn("[switch]$PreservePowerPlan", self.script)
        for command in (
            '@("/SETACVALUEINDEX", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE", "1")',
            '@("/SETDCVALUEINDEX", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE", "1")',
            '@("/SETACVALUEINDEX", "SCHEME_CURRENT", "SUB_SLEEP", "HYBRIDSLEEP", "0")',
            '@("/SETDCVALUEINDEX", "SCHEME_CURRENT", "SUB_SLEEP", "HYBRIDSLEEP", "0")',
        ):
            self.assertIn(command, self.script)
        self.assertIn("power_plan_prepared = $powerPlanPrepared", self.script)

    def test_start_is_idempotent_for_an_already_running_task(self) -> None:
        self.assertIn('$registeredTask.State -ne "Running"', self.script)
        self.assertIn("started_now = $startedNow", self.script)

    def test_host_reuses_the_primary_api_port(self) -> None:
        self.assertIn("[int]$Port = 8000", self.script)


if __name__ == "__main__":
    unittest.main()
