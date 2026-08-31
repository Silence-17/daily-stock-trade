# -*- coding: utf-8 -*-
"""Regression tests for RuntimeSchedulerService scheduling ownership."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.services.runtime_scheduler import (
    CLI_SCHEDULER_OWNER_ENV,
    RUNTIME_SCHEDULER_ARGS_ENV,
    RUNTIME_SCHEDULER_DISABLE_DAILY_ENV,
    RUNTIME_SCHEDULER_FORCE_ENABLED_ENV,
    RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV,
    RUNTIME_SCHEDULER_SUPPRESS_START_ENV,
    RuntimeSchedulerService,
)


_MISSING_MODULE = object()


def _disabled_vnpy_runtime_handle() -> SimpleNamespace:
    return SimpleNamespace(
        main_engine=None,
        event_engine=None,
        event_bridge=None,
        diagnostics={"enabled": False, "reason": "disabled"},
        close=lambda: None,
    )


@contextmanager
def _patched_sys_module(name: str, module):
    """Replace one module key without rolling back unrelated lazy imports."""

    previous = sys.modules.get(name, _MISSING_MODULE)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class _FakeJob:
    def __init__(self, schedule_module):
        self._schedule_module = schedule_module
        self.next_run = datetime(2026, 1, 1, 18, 0, 0)
        self.at_time = None
        self.job_func = None

    @property
    def day(self):
        return self

    def at(self, value):
        self.at_time = value
        hour, minute = [int(part) for part in value.split(":")]
        self.next_run = datetime(2026, 1, 1, hour, minute, 0)
        return self

    def do(self, fn):
        self.job_func = fn
        self._schedule_module.jobs.append(self)
        return self


class _FakeScheduleModule:
    def __init__(self):
        self.jobs = []

    def every(self):
        return _FakeJob(self)

    def get_jobs(self):
        return list(self.jobs)

    def run_pending(self):
        for job in list(self.jobs):
            job.job_func()

    def cancel_job(self, job):
        if job in self.jobs:
            self.jobs.remove(job)


class _NoopThread:
    def __init__(self, target=None, **kwargs):
        self.target = target
        self.kwargs = kwargs

    def start(self):
        return None

    def is_alive(self):
        return False


class _SynchronousThread(_NoopThread):
    def start(self):
        if self.target is not None:
            self.target()


class _FakeTaskEventRepository:
    def __init__(self, initial_events=None):
        self.events = list(initial_events or [])
        self.cleanup_calls = []

    def record_task_event(
        self,
        *,
        name,
        status,
        message,
        details=None,
        duration_seconds=None,
        timestamp=None,
    ):
        event = {
            "name": name,
            "status": status,
            "message": message,
            "timestamp": timestamp.isoformat() if timestamp is not None else None,
            "details": details or {},
        }
        if duration_seconds is not None:
            event["duration_seconds"] = duration_seconds
        self.events.append(event)
        return event

    def cleanup_task_events(self, *, older_than):
        self.cleanup_calls.append(older_than)
        return 0

    def list_task_events(self, *, name=None, status=None, limit=50, started_at=None):
        events = list(self.events)
        if name:
            events = [event for event in events if event["name"] == name]
        if status:
            events = [event for event in events if event["status"] == status]
        if started_at is not None:
            cutoff = started_at.isoformat()
            events = [event for event in events if str(event.get("timestamp") or "") >= cutoff]
        return events[-limit:]


class RuntimeSchedulerServiceTestCase(unittest.TestCase):
    def test_background_task_result_summary_keeps_bounded_shadow_evidence(self) -> None:
        details = RuntimeSchedulerService._summarize_background_task_result({
            "accepted": True,
            "configured_count": 2,
            "completed_count": 2,
            "submitted_count": 0,
            "execution_mode": "dry_run",
            "trigger_source": "cross_market_intraday_entry_scan",
            "analysis_slot": "13:30",
            "formal_recovery": False,
            "intraday_entry_recheck": True,
            "retryable": True,
            "retry_after_seconds": 30,
            "submits_orders": False,
            "runs": [
                {
                    "market": "cn",
                    "strategy": "dual_low",
                    "agent_run_uid": "run-cn",
                    "submitted_count": 0,
                    "private_payload": "excluded",
                },
                {
                    "market": "us",
                    "strategy": "us_large_cap_momentum",
                    "agent_run_uid": "run-us",
                    "submitted_count": 0,
                },
            ],
            "failures": [],
            "cadence_skips": [
                {
                    "market": "hk",
                    "strategy": "hk_liquid_momentum",
                    "reason": "calibration_interval_not_elapsed",
                    "latest_run_uid": f"shadow-{index}",
                    "latest_created_at": "2026-07-21T02:56:15",
                    "age_seconds": 3600.0,
                    "required_interval_seconds": 86400,
                    "scheduler_grace_seconds": 300,
                    "required_elapsed_seconds": 86100,
                    "remaining_seconds": 82500.0,
                    "next_eligible_at": "2026-07-22T02:51:15",
                    "private_payload": "excluded",
                }
                for index in range(12)
            ],
            "unbounded_payload": "excluded",
        })

        self.assertEqual(details["configured_count"], 2)
        self.assertEqual(details["completed_count"], 2)
        self.assertEqual(details["submitted_count"], 0)
        self.assertEqual(details["execution_mode"], "dry_run")
        self.assertEqual(
            details["trigger_source"],
            "cross_market_intraday_entry_scan",
        )
        self.assertEqual(details["analysis_slot"], "13:30")
        self.assertFalse(details["formal_recovery"])
        self.assertTrue(details["intraday_entry_recheck"])
        self.assertTrue(details["retryable"])
        self.assertEqual(details["retry_after_seconds"], 30)
        self.assertFalse(details["submits_orders"])
        self.assertEqual(details["run_count"], 2)
        self.assertEqual(details["failure_count"], 0)
        self.assertEqual(details["cadence_skipped_count"], 12)
        self.assertEqual(len(details["cadence_skips"]), 10)
        self.assertEqual(details["cadence_skips"][0]["latest_run_uid"], "shadow-0")
        self.assertNotIn("private_payload", details["cadence_skips"][0])
        self.assertNotIn("private_payload", details["runs"][0])
        self.assertNotIn("unbounded_payload", details)

    def test_background_task_failure_keeps_sanitized_exception_evidence(self) -> None:
        class PartialFailure(RuntimeError):
            def __init__(self) -> None:
                super().__init__("one pair failed")
                self.details = {
                    "configured_count": 2,
                    "completed_count": 1,
                    "failed_count": 1,
                    "submitted_count": 0,
                    "submits_orders": False,
                    "runs": [{"market": "cn", "strategy": "dual_low"}],
                    "failures": [{
                        "market": "us",
                        "strategy": "us_large_cap_momentum",
                        "error": "provider unavailable",
                        "secret": "excluded",
                    }],
                }

        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
        )
        wrapped = service._instrument_background_task(
            "agent_calibration_shadow",
            lambda: (_ for _ in ()).throw(PartialFailure()),
        )

        with self.assertRaisesRegex(PartialFailure, "one pair failed"):
            wrapped()

        event = service.task_events(name="agent_calibration_shadow", limit=1)[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["details"]["completed_count"], 1)
        self.assertEqual(event["details"]["failed_count"], 1)
        self.assertEqual(event["details"]["submitted_count"], 0)
        self.assertFalse(event["details"]["submits_orders"])
        self.assertEqual(event["details"]["failures"][0]["market"], "us")
        self.assertNotIn("secret", event["details"]["failures"][0])

    def test_background_task_result_summary_keeps_bounded_calibration_evidence(self) -> None:
        details = RuntimeSchedulerService._summarize_background_task_result({
            "accepted": True,
            "reason": "calibration_evidence_pending",
            "evidence_ready": False,
            "read_only": True,
            "creates_agent_runs": False,
            "places_orders": False,
            "window_days": 90,
            "alert_transition_recorded": True,
            "alert_transition_reason": "calibration_evidence_pending",
            "previous_evidence_ready": None,
            "evidence_failures": [f"cn:failure-{index}" for index in range(45)],
            "market_evidence": [{
                "market": "cn",
                "ok": False,
                "failures": [f"failure-{index}-{'x' * 140}" for index in range(15)],
                "total_runs": 9,
                "observed_runs": 9,
                "observation_rate_pct": 100.0,
                "latest_mature_sample_count": 0,
                "observation_days": 2,
                "latest_age_hours": 1.0,
                "latest_state": "insufficient_evidence",
                "private_payload": "excluded",
            }],
            "thresholds": {
                "min_runs_per_market": 20,
                "min_mature_samples": 20,
                "secret": "excluded",
            },
        })

        self.assertFalse(details["evidence_ready"])
        self.assertTrue(details["read_only"])
        self.assertFalse(details["creates_agent_runs"])
        self.assertFalse(details["places_orders"])
        self.assertTrue(details["alert_transition_recorded"])
        self.assertEqual(
            details["alert_transition_reason"],
            "calibration_evidence_pending",
        )
        self.assertIsNone(details["previous_evidence_ready"])
        self.assertEqual(details["failure_count"], 45)
        self.assertEqual(len(details["evidence_failures"]), 40)
        self.assertEqual(details["market_count"], 1)
        self.assertEqual(len(details["market_evidence"][0]["failures"]), 12)
        self.assertTrue(all(
            len(item) <= 120
            for item in details["market_evidence"][0]["failures"]
        ))
        self.assertNotIn("private_payload", details["market_evidence"][0])
        self.assertNotIn("secret", details["thresholds"])

    def test_vnpy_background_tasks_receive_runtime_engine_dependencies(self) -> None:
        config = SimpleNamespace(schedule_enabled=False)
        main_engine = object()
        event_engine = object()
        service = RuntimeSchedulerService(config_provider=lambda: config)
        service.set_vnpy_runtime_engines(
            main_engine=main_engine,
            event_engine=event_engine,
        )
        build_tasks = MagicMock(return_value=[])
        fake_module = ModuleType("src.services.vnpy_paper_trading_service")
        fake_module.build_vnpy_paper_trading_background_tasks = build_tasks

        with _patched_sys_module(
            "src.services.vnpy_paper_trading_service",
            fake_module,
        ):
            tasks = service._current_vnpy_paper_trading_background_tasks(config)

        self.assertEqual(tasks, [])
        build_tasks.assert_called_once_with(
            vnpy_main_engine=main_engine,
            vnpy_event_engine=event_engine,
        )

    def test_run_analysis_args_include_workers(self) -> None:
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        seen_args = []

        def runner(config_arg, args, stock_codes):
            seen_args.append(args)

        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            task_runner=runner,
        )
        service._reload_config = lambda: config

        service._run_analysis_once()

        self.assertEqual(len(seen_args), 1)
        self.assertTrue(hasattr(seen_args[0], "workers"))
        self.assertIsNone(seen_args[0].workers)

    def test_run_analysis_args_preserve_startup_schedule_flags(self) -> None:
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        seen_args = []

        def runner(config_arg, args, stock_codes):
            seen_args.append(args)

        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            task_runner=runner,
            schedule_args_overrides={
                "no_notify": True,
                "no_market_review": True,
                "dry_run": True,
                "force_run": True,
                "single_notify": True,
                "no_context_snapshot": True,
                "workers": 3,
                "serve": True,
            },
        )
        service._reload_config = lambda: config

        service._run_analysis_once()

        self.assertEqual(len(seen_args), 1)
        self.assertTrue(seen_args[0].no_notify)
        self.assertTrue(seen_args[0].no_market_review)
        self.assertTrue(seen_args[0].dry_run)
        self.assertTrue(seen_args[0].force_run)
        self.assertTrue(seen_args[0].single_notify)
        self.assertTrue(seen_args[0].no_context_snapshot)
        self.assertEqual(seen_args[0].workers, 3)
        self.assertFalse(seen_args[0].serve)
        self.assertTrue(seen_args[0].serve_only)

    def test_default_runner_does_not_mark_failed_analysis_return_success(self) -> None:
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        service = RuntimeSchedulerService(config_provider=lambda: config)
        service._reload_config = lambda: config

        with patch("main.run_full_analysis", return_value=False) as run_full_analysis:
            service._run_analysis_once()

        run_full_analysis.assert_called_once()
        self.assertTrue(run_full_analysis.call_args.kwargs["raise_errors"])
        status = service.status()
        self.assertIsNone(status["last_success_at"])
        self.assertIn("reported failure", status["last_error"])

    def test_run_now_rejects_when_analysis_is_already_running(self) -> None:
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        service = RuntimeSchedulerService(config_provider=lambda: config)
        service._run_lock.acquire()
        try:
            result = service.run_now()
        finally:
            service._run_lock.release()

        self.assertFalse(result["accepted"])
        self.assertTrue(result["running"])
        self.assertEqual(result["reason"], "analysis_already_running")
        status = service.status()
        self.assertEqual(status["last_skip_reason"], "analysis_already_running")
        self.assertIsNotNone(status["last_skipped_at"])

    def test_run_now_runs_analysis_with_default_stock_scope(self) -> None:
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        seen_stock_codes = []

        def runner(config_arg, args, stock_codes):
            seen_stock_codes.append(stock_codes)
            return True

        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            task_runner=runner,
        )
        service._reload_config = lambda: config

        with patch(
            "src.services.runtime_scheduler.threading.Thread",
            _SynchronousThread,
        ):
            result = service.run_now()

        self.assertTrue(result["accepted"])
        self.assertEqual(seen_stock_codes, [None])
        status = service.status()
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["last_run_at"])
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNone(status["last_error"])

    def test_run_now_uses_shared_lock_across_service_instances(self) -> None:
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        primary_service = RuntimeSchedulerService(config_provider=lambda: config)
        secondary_service = RuntimeSchedulerService(config_provider=lambda: config)

        self.assertIs(primary_service._run_lock, secondary_service._run_lock)

        primary_service._run_lock.acquire()
        try:
            result = secondary_service.run_now()
        finally:
            primary_service._run_lock.release()

        self.assertFalse(result["accepted"])
        self.assertEqual(result["running"], True)
        self.assertEqual(result["reason"], "analysis_already_running")
        status = secondary_service.status()
        self.assertEqual(status["last_skip_reason"], "analysis_already_running")
        self.assertIsNotNone(status["last_skipped_at"])

    def test_run_now_endpoint_returns_conflict_when_scheduler_is_busy(self) -> None:
        from api.v1.endpoints.system_config import run_scheduler_now

        scheduler = MagicMock()
        scheduler.run_now.return_value = {
            "accepted": False,
            "running": True,
            "reason": "analysis_already_running",
        }

        with self.assertRaises(HTTPException) as captured:
            run_scheduler_now(scheduler=scheduler)

        self.assertEqual(captured.exception.status_code, 409)
        self.assertEqual(captured.exception.detail["error"], "scheduler_busy")
        self.assertEqual(captured.exception.detail["reason"], "analysis_already_running")

    def test_reconcile_replaces_daily_jobs_without_triggering_old_jobs(self) -> None:
        fake_schedule = _FakeScheduleModule()
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["09:20"],
        )
        calls = []

        def runner(config_arg, args, stock_codes):
            calls.append("run")

        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            task_runner=runner,
        )
        service._reload_config = lambda: config

        with _patched_sys_module("schedule", fake_schedule), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ):
            service.reconcile_from_config()
            old_jobs = fake_schedule.get_jobs()
            self.assertEqual([job.at_time for job in old_jobs], ["09:20"])

            config.schedule_times = ["15:10"]
            service.reconcile_from_config()

            self.assertEqual([job.at_time for job in fake_schedule.get_jobs()], ["15:10"])
            self.assertNotIn(old_jobs[0], fake_schedule.get_jobs())

            fake_schedule.run_pending()

        self.assertEqual(calls, ["run"])

    def test_initial_reconcile_can_run_immediately_once(self) -> None:
        fake_schedule = _FakeScheduleModule()
        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["09:20"],
        )
        calls = []

        def runner(config_arg, args, stock_codes):
            calls.append("run")

        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            task_runner=runner,
        )
        service._reload_config = lambda: config

        with _patched_sys_module("schedule", fake_schedule), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ):
            service.reconcile_from_config(run_immediately=True)
            config.schedule_times = ["15:10"]
            service.reconcile_from_config()

        self.assertEqual(calls, ["run"])

    def test_start_registers_event_monitor_background_task(self) -> None:
        class _FakeScheduler:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.background_tasks = []
                self.daily_task = None
                self.daily_task_run_immediately = None
                self._jobs = []

            def set_daily_task(self, task, run_immediately: bool) -> None:
                self.daily_task = task
                self.daily_task_run_immediately = run_immediately

            def add_background_task(
                self,
                task: callable,
                interval_seconds: int,
                run_immediately: bool,
                name: str | None = None,
                initial_delay_seconds: int | None = None,
                next_delay_seconds_provider=None,
            ) -> None:
                self.background_tasks.append({
                    "task": task,
                    "interval_seconds": interval_seconds,
                    "initial_delay_seconds": initial_delay_seconds,
                    "run_immediately": run_immediately,
                    "name": name,
                })

            def run(self) -> None:
                return None

            def stop(self) -> None:
                return None

            @property
            def schedule(self):
                class _Namespace:
                    @staticmethod
                    def get_jobs():
                        return []

                return _Namespace

            @property
            def schedule_time(self):
                return self.kwargs.get("schedule_time")

        fake_worker = MagicMock()
        fake_worker.run_once.return_value = {"triggered": 2}

        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
            agent_event_monitor_enabled=True,
            agent_event_monitor_interval_minutes=7,
        )

        service = RuntimeSchedulerService(config_provider=lambda: config)
        service._reload_config = lambda: config

        with patch(
            "src.services.runtime_scheduler.Scheduler",
            _FakeScheduler,
        ), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ), patch.object(
            RuntimeSchedulerService,
            "_current_vnpy_paper_trading_background_tasks",
            return_value=[],
        ), patch("src.services.alert_worker.AlertWorker", return_value=fake_worker):
            service.start()

        scheduler = service._scheduler
        self.assertIsNotNone(scheduler)
        self.assertEqual(len(scheduler.background_tasks), 1)  # type: ignore[attr-defined]
        self.assertEqual(scheduler.background_tasks[0]["name"], "agent_event_monitor")  # type: ignore[index]
        self.assertEqual(scheduler.background_tasks[0]["interval_seconds"], 7 * 60)  # type: ignore[index]
        self.assertEqual(scheduler.background_tasks[0]["run_immediately"], True)  # type: ignore[index]
        scheduler.background_tasks[0]["task"]()  # type: ignore[index]
        fake_worker.run_once.assert_called_once()

    def test_start_can_run_background_tasks_without_daily_schedule(self) -> None:
        class _FakeScheduler:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.background_tasks = []
                self.daily_task = None
                self.daily_task_run_immediately = None

            def set_daily_task(self, task, run_immediately: bool) -> None:
                self.daily_task = task
                self.daily_task_run_immediately = run_immediately

            def add_background_task(
                self,
                task: callable,
                interval_seconds: int,
                run_immediately: bool,
                name: str | None = None,
                initial_delay_seconds: int | None = None,
                next_delay_seconds_provider=None,
            ) -> None:
                self.background_tasks.append({
                    "task": task,
                    "interval_seconds": interval_seconds,
                    "initial_delay_seconds": initial_delay_seconds,
                    "last_run": 1000.0,
                    "run_immediately": run_immediately,
                    "name": name,
                })

            def run(self) -> None:
                return None

            def stop(self) -> None:
                return None

            @property
            def schedule(self):
                class _Namespace:
                    @staticmethod
                    def get_jobs():
                        return []

                return _Namespace

        config = SimpleNamespace(
            schedule_enabled=False,
            schedule_time="18:00",
            schedule_times=["18:00"],
        )
        background_task = MagicMock()
        background_task.return_value = {
            "skipped": True,
            "reason": "auto_trade_disabled",
            "submitted_count": 0,
            "skipped_count": 0,
        }
        task_event_repo = _FakeTaskEventRepository()
        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            background_tasks_provider=lambda _config: [{
                "task": background_task,
                "interval_seconds": 300,
                "run_immediately": False,
                "name": "vnpy_paper_auto_trade",
                "initial_delay_seconds": 120,
            }],
            task_event_repository=task_event_repo,
        )

        with patch(
            "src.services.runtime_scheduler.Scheduler",
            _FakeScheduler,
        ), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ):
            service.reconcile_from_config()

        scheduler = service._scheduler
        self.assertIsNotNone(scheduler)
        status = service.status()
        self.assertTrue(status["enabled"])
        self.assertFalse(status["loop_running"])
        self.assertEqual(status["background_tasks"][0]["name"], "vnpy_paper_auto_trade")
        self.assertEqual(status["background_tasks"][0]["interval_seconds"], 300)
        self.assertEqual(status["background_tasks"][0]["initial_delay_seconds"], 120)
        self.assertFalse(status["background_tasks"][0]["running"])
        self.assertEqual(status["background_tasks"][0]["next_run_at"], datetime.fromtimestamp(1300.0).isoformat())
        self.assertEqual(status["next_run_at"], datetime.fromtimestamp(1300.0).isoformat())
        self.assertIsNone(scheduler.daily_task)  # type: ignore[attr-defined]
        self.assertEqual(len(scheduler.background_tasks), 1)  # type: ignore[attr-defined]
        self.assertEqual(scheduler.background_tasks[0]["name"], "vnpy_paper_auto_trade")  # type: ignore[index]
        scheduler.background_tasks[0]["task"]()  # type: ignore[index]
        task_events = service.status()["task_events"]
        self.assertEqual([event["status"] for event in task_events], ["skipped"])
        self.assertEqual(task_events[-1]["name"], "vnpy_paper_auto_trade")
        self.assertEqual(task_events[-1]["details"]["reason"], "auto_trade_disabled")
        self.assertEqual(task_events[-1]["details"]["submitted_count"], 0)
        self.assertEqual([event["status"] for event in task_event_repo.events], ["skipped"])
        filtered_events = service.task_events(
            name="vnpy_paper_auto_trade",
            status="skipped",
            limit=10,
        )
        self.assertEqual(len(filtered_events), 1)
        self.assertEqual(filtered_events[0]["status"], "skipped")
        self.assertEqual(filtered_events[0]["details"]["reason"], "auto_trade_disabled")

    def test_task_events_can_read_persisted_repository_events(self) -> None:
        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            task_event_repository=_FakeTaskEventRepository([{
                "name": "vnpy_paper_auto_retry",
                "status": "failed",
                "message": "persisted failure",
                "timestamp": "2026-07-02T09:32:00",
                "details": {"error": "boom"},
            }]),
        )

        events = service.task_events(
            name="vnpy_paper_auto_retry",
            status="failed",
            limit=10,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "persisted failure")
        self.assertEqual(events[0]["details"]["error"], "boom")

    def test_background_task_overlap_guard_survives_wrapper_replacement(self) -> None:
        repo = _FakeTaskEventRepository()
        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            task_event_repository=repo,
        )
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        def first_generation_task():
            calls.append("first")
            first_started.set()
            self.assertTrue(release_first.wait(timeout=5))
            return {"accepted": True, "submitted_count": 0}

        def second_generation_task():
            calls.append("second")
            return {"accepted": True, "submitted_count": 0}

        first_wrapper = service._instrument_background_task(
            "vnpy_paper_auto_retry",
            first_generation_task,
        )
        worker = threading.Thread(target=first_wrapper)
        worker.start()
        self.assertTrue(first_started.wait(timeout=5))

        second_wrapper = service._instrument_background_task(
            "vnpy_paper_auto_retry",
            second_generation_task,
        )
        skipped = second_wrapper()

        self.assertEqual(calls, ["first"])
        self.assertEqual(skipped["reason"], "task_already_running")
        self.assertTrue(skipped["skipped"])
        self.assertEqual(skipped["overlap_guard"], "process_task_name")

        fake_schedule = SimpleNamespace(get_jobs=lambda: [])
        service._scheduler = SimpleNamespace(
            schedule=fake_schedule,
            schedule_times=[],
            _background_tasks=[{
                "name": "vnpy_paper_auto_retry",
                "interval_seconds": 300,
                "last_run": 1000.0,
                "thread": None,
                "running": False,
            }],
        )
        guarded_status = service.status()["background_tasks"][0]
        self.assertTrue(guarded_status["running"])
        self.assertTrue(guarded_status["overlap_guarded"])
        self.assertTrue(guarded_status["previous_generation_running"])

        release_first.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        completed = second_wrapper()

        self.assertTrue(completed["accepted"])
        self.assertEqual(calls, ["first", "second"])
        recovered_status = service.status()["background_tasks"][0]
        self.assertFalse(recovered_status["running"])
        self.assertTrue(recovered_status["overlap_guarded"])
        self.assertFalse(recovered_status["previous_generation_running"])
        self.assertEqual(
            [event["status"] for event in repo.events],
            ["skipped", "completed", "completed"],
        )

    def test_background_task_overlap_guard_releases_after_exception(self) -> None:
        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
        )

        def failing_task():
            raise RuntimeError("unit failure")

        failing_wrapper = service._instrument_background_task(
            "vnpy_paper_auto_trade",
            failing_task,
        )
        with self.assertRaisesRegex(RuntimeError, "unit failure"):
            failing_wrapper()

        healthy_task = MagicMock(return_value={"accepted": True})
        healthy_wrapper = service._instrument_background_task(
            "vnpy_paper_auto_trade",
            healthy_task,
        )
        result = healthy_wrapper()

        self.assertTrue(result["accepted"])
        healthy_task.assert_called_once_with()
        self.assertEqual(
            [event["status"] for event in service.task_events(limit=10)],
            ["failed", "completed"],
        )

    def test_immediate_background_task_runs_once_per_registration_lifetime(self) -> None:
        enabled = True
        task = MagicMock(return_value={"accepted": True})

        def tasks_provider(_config):
            if not enabled:
                return []
            return [{
                "task": task,
                "interval_seconds": 300,
                "run_immediately": True,
                "name": "vnpy_paper_auto_retry",
            }]

        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            background_tasks_provider=tasks_provider,
        )

        first = service._current_background_tasks(SimpleNamespace())
        second = service._current_background_tasks(SimpleNamespace())
        enabled = False
        disabled = service._current_background_tasks(SimpleNamespace())
        enabled = True
        reenabled = service._current_background_tasks(SimpleNamespace())

        self.assertTrue(first[0]["run_immediately"])
        self.assertFalse(second[0]["run_immediately"])
        self.assertEqual(disabled, [])
        self.assertTrue(reenabled[0]["run_immediately"])

    def test_background_task_registration_is_independent_per_task_name(self) -> None:
        auto_trade = MagicMock(return_value={"accepted": True})
        auto_retry = MagicMock(return_value={"accepted": True})
        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            background_tasks_provider=lambda _config: [
                {
                    "task": auto_trade,
                    "interval_seconds": 300,
                    "run_immediately": False,
                    "name": "vnpy_paper_auto_trade",
                },
                {
                    "task": auto_retry,
                    "interval_seconds": 300,
                    "run_immediately": True,
                    "name": "vnpy_paper_auto_retry",
                },
            ],
        )

        first = service._current_background_tasks(SimpleNamespace())
        second = service._current_background_tasks(SimpleNamespace())

        self.assertEqual(
            {entry["name"]: entry["run_immediately"] for entry in first},
            {
                "vnpy_paper_auto_trade": False,
                "vnpy_paper_auto_retry": True,
            },
        )
        self.assertEqual(
            {entry["name"]: entry["run_immediately"] for entry in second},
            {
                "vnpy_paper_auto_trade": False,
                "vnpy_paper_auto_retry": False,
            },
        )

    def test_reconcile_only_restarts_immediate_task_after_reregistration(self) -> None:
        class _ImmediateScheduler:
            def __init__(self, **_kwargs):
                self.background_tasks = []
                self.schedule_times = []

            def add_background_task(
                self,
                task,
                interval_seconds,
                run_immediately,
                name=None,
                initial_delay_seconds=None,
                next_delay_seconds_provider=None,
            ):
                self.background_tasks.append({
                    "task": task,
                    "interval_seconds": interval_seconds,
                    "run_immediately": run_immediately,
                    "name": name,
                    "initial_delay_seconds": initial_delay_seconds,
                })
                if run_immediately:
                    task()

            def run(self):
                return None

            def stop(self):
                return None

            @property
            def schedule(self):
                return SimpleNamespace(get_jobs=lambda: [])

        enabled = True
        auto_retry = MagicMock(return_value={
            "accepted": True,
            "attempted_count": 0,
            "submitted_count": 0,
        })

        def tasks_provider(_config):
            if not enabled:
                return []
            return [{
                "task": auto_retry,
                "interval_seconds": 300,
                "run_immediately": True,
                "name": "vnpy_paper_auto_retry",
            }]

        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            background_tasks_provider=tasks_provider,
        )

        with patch(
            "src.services.runtime_scheduler.Scheduler",
            _ImmediateScheduler,
        ), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ):
            service.reconcile_from_config()
            service.reconcile_from_config()
            self.assertEqual(auto_retry.call_count, 1)

            enabled = False
            service.reconcile_from_config()
            enabled = True
            service.reconcile_from_config()

        self.assertEqual(auto_retry.call_count, 2)
        self.assertEqual(
            [event["status"] for event in service.task_events(limit=10)],
            ["completed", "completed"],
        )

    def test_expected_window_skip_does_not_persist_task_event(self) -> None:
        repo = _FakeTaskEventRepository()
        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            task_event_repository=repo,
        )
        for name, reason in (
            (
                "cross_market_intraday_entry_scan",
                "outside_cross_market_entry_analysis_slot",
            ),
            ("vnpy_paper_auto_retry", "no_retry_work"),
            (
                "cross_market_pending_order_revalidation",
                "no_pending_order_changes",
            ),
            (
                "cross_market_intraday_sell_monitor",
                "no_cross_market_sellable_positions",
            ),
        ):
            wrapped = service._instrument_background_task(
                name,
                lambda reason=reason: {
                    "accepted": True,
                    "skipped": True,
                    "reason": reason,
                },
            )
            wrapped()

        self.assertEqual(repo.events, [])

    def test_background_task_event_persistence_triggers_retention_cleanup(self) -> None:
        repo = _FakeTaskEventRepository()
        service = RuntimeSchedulerService(
            config_provider=lambda: SimpleNamespace(schedule_enabled=False),
            task_event_repository=repo,
            task_event_retention_days=7,
            task_event_cleanup_interval_seconds=0,
        )

        service._record_background_task_event(
            name="vnpy_paper_auto_trade",
            status="completed",
            message="done",
        )

        self.assertEqual(len(repo.cleanup_calls), 1)
        cutoff = repo.cleanup_calls[0]
        self.assertLessEqual(cutoff, datetime.now() - timedelta(days=7))
        self.assertGreater(cutoff, datetime.now() - timedelta(days=7, seconds=5))

    def test_rebuild_reuses_event_monitor_without_immediate_rerun(self) -> None:
        schedulers = []

        class _FakeScheduler:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.background_tasks = []
                self.daily_task = None
                self.daily_task_run_immediately = None
                self._jobs = []
                schedulers.append(self)

            def set_daily_task(self, task, run_immediately: bool) -> None:
                self.daily_task = task
                self.daily_task_run_immediately = run_immediately

            def add_background_task(
                self,
                task: callable,
                interval_seconds: int,
                run_immediately: bool,
                name: str | None = None,
                initial_delay_seconds: int | None = None,
                next_delay_seconds_provider=None,
            ) -> None:
                self.background_tasks.append({
                    "task": task,
                    "interval_seconds": interval_seconds,
                    "initial_delay_seconds": initial_delay_seconds,
                    "run_immediately": run_immediately,
                    "name": name,
                })

            def run(self) -> None:
                return None

            def stop(self) -> None:
                return None

            @property
            def schedule(self):
                class _Namespace:
                    @staticmethod
                    def get_jobs():
                        return []

                return _Namespace

            @property
            def schedule_time(self):
                return self.kwargs.get("schedule_time")

        fake_worker = MagicMock()
        fake_worker.run_once.return_value = {"triggered": 0}

        config = SimpleNamespace(
            schedule_enabled=True,
            schedule_time="18:00",
            schedule_times=["18:00"],
            agent_event_monitor_enabled=True,
            agent_event_monitor_interval_minutes=7,
        )

        service = RuntimeSchedulerService(config_provider=lambda: config)
        service._reload_config = lambda: config

        with patch(
            "src.services.runtime_scheduler.Scheduler",
            _FakeScheduler,
        ), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ), patch("src.services.alert_worker.AlertWorker", return_value=fake_worker) as worker_cls:
            service.reconcile_from_config()
            config.schedule_times = ["19:00"]
            config.agent_event_monitor_interval_minutes = 11
            service.reconcile_from_config()
            config.schedule_times = ["20:00"]
            service.reconcile_from_config()

        self.assertEqual(worker_cls.call_count, 1)
        self.assertEqual(len(schedulers), 3)
        first_task = schedulers[0].background_tasks[0]
        second_task = schedulers[1].background_tasks[0]
        third_task = schedulers[2].background_tasks[0]
        self.assertTrue(first_task["run_immediately"])
        self.assertFalse(second_task["run_immediately"])
        self.assertFalse(third_task["run_immediately"])
        self.assertIs(first_task["task"], second_task["task"])
        self.assertIs(first_task["task"], third_task["task"])
        self.assertEqual(second_task["interval_seconds"], 11 * 60)
        self.assertEqual(third_task["interval_seconds"], 11 * 60)

    def test_force_enabled_survives_time_reconcile_until_explicit_enabled_update(self) -> None:
        fake_schedule = _FakeScheduleModule()
        config = SimpleNamespace(
            schedule_enabled=False,
            schedule_time="18:00",
            schedule_times=["09:20"],
        )
        service = RuntimeSchedulerService(
            config_provider=lambda: config,
            force_enabled=True,
            background_tasks_provider=lambda _config: [],
        )

        with _patched_sys_module("schedule", fake_schedule), patch(
            "src.services.runtime_scheduler.threading.Thread",
            _NoopThread,
        ):
            service.reconcile_from_config()
            self.assertTrue(service.status()["enabled"])

            config.schedule_times = ["15:10"]
            service.reconcile_from_config()
            self.assertTrue(service.status()["enabled"])
            self.assertEqual([job.at_time for job in fake_schedule.get_jobs()], ["15:10"])

            service.reconcile_from_config(clear_enabled_override=True)
            self.assertFalse(service.status()["enabled"])
            self.assertEqual(fake_schedule.get_jobs(), [])

    def test_lifespan_disables_runtime_scheduler_when_cli_owns_schedule(self) -> None:
        from api.app import create_app

        events = []

        class FakeRuntimeSchedulerService:
            def __init__(
                self,
                *,
                owns_schedule=True,
                force_enabled=False,
                daily_schedule_disabled=False,
                run_immediately_in_background=False,
                schedule_args_overrides=None,
                task_event_repository=None,
            ):
                self.owns_schedule = owns_schedule
                self.force_enabled = force_enabled
                events.append(("init", owns_schedule, force_enabled, run_immediately_in_background))

            def reconcile_from_config(self, *, run_immediately=False, clear_enabled_override=False):
                events.append((
                    "reconcile",
                    self.owns_schedule,
                    run_immediately,
                    clear_enabled_override,
                ))

            def stop(self):
                events.append(("stop", self.owns_schedule))

        class FakeSystemConfigService:
            def __init__(self, runtime_scheduler=None):
                self.runtime_scheduler = runtime_scheduler

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {CLI_SCHEDULER_OWNER_ENV: "true"},
            clear=False,
        ), patch(
            "src.config.get_config",
            return_value=SimpleNamespace(schedule_run_immediately=True),
        ), patch("api.app.RuntimeSchedulerService", FakeRuntimeSchedulerService), patch(
            "api.app.SystemConfigService",
            FakeSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=_disabled_vnpy_runtime_handle(),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            app = create_app(static_dir=Path(temp_dir))
            with TestClient(app):
                pass

        self.assertEqual(events, [
            ("init", False, False, True),
            ("reconcile", False, False, False),
            ("stop", False),
        ])

    def test_lifespan_passes_runtime_scheduler_start_flags(self) -> None:
        from api.app import create_app

        events = []

        class FakeRuntimeSchedulerService:
            def __init__(
                self,
                *,
                owns_schedule=True,
                force_enabled=False,
                daily_schedule_disabled=False,
                run_immediately_in_background=False,
                schedule_args_overrides=None,
                task_event_repository=None,
            ):
                events.append(("init", owns_schedule, force_enabled, run_immediately_in_background))

            def reconcile_from_config(self, *, run_immediately=False, clear_enabled_override=False):
                events.append(("reconcile", run_immediately, clear_enabled_override))

            def stop(self):
                events.append(("stop",))

        class FakeSystemConfigService:
            def __init__(self, runtime_scheduler=None):
                self.runtime_scheduler = runtime_scheduler

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                RUNTIME_SCHEDULER_FORCE_ENABLED_ENV: "true",
                RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV: "true",
            },
            clear=False,
        ), patch("api.app.RuntimeSchedulerService", FakeRuntimeSchedulerService), patch(
            "api.app.SystemConfigService",
            FakeSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=_disabled_vnpy_runtime_handle(),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            app = create_app(static_dir=Path(temp_dir))
            with TestClient(app):
                pass

        self.assertEqual(events, [
            ("init", True, True, True),
            ("reconcile", True, False),
            ("stop",),
        ])
        self.assertIsNone(os.getenv(RUNTIME_SCHEDULER_FORCE_ENABLED_ENV))
        self.assertIsNone(os.getenv(RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV))

    def test_lifespan_can_disable_daily_schedule_without_suppressing_start(self) -> None:
        from api.app import create_app

        events = []
        main_engine = object()
        event_engine = object()

        class FakeRuntimeSchedulerService:
            def __init__(
                self,
                *,
                owns_schedule=True,
                force_enabled=False,
                daily_schedule_disabled=False,
                run_immediately_in_background=False,
                schedule_args_overrides=None,
                task_event_repository=None,
            ):
                events.append(("init", owns_schedule, force_enabled, daily_schedule_disabled))

            def reconcile_from_config(self, *, run_immediately=False, clear_enabled_override=False):
                events.append(("reconcile", run_immediately, clear_enabled_override))

            def set_vnpy_runtime_engines(self, *, main_engine=None, event_engine=None):
                events.append(("bind_vnpy", main_engine, event_engine))

            def stop(self):
                events.append(("stop",))

        class FakeSystemConfigService:
            def __init__(self, runtime_scheduler=None):
                self.runtime_scheduler = runtime_scheduler

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {RUNTIME_SCHEDULER_DISABLE_DAILY_ENV: "true"},
            clear=False,
        ), patch(
            "src.config.get_config",
            return_value=SimpleNamespace(schedule_run_immediately=False),
        ), patch("api.app.RuntimeSchedulerService", FakeRuntimeSchedulerService), patch(
            "api.app.SystemConfigService",
            FakeSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=SimpleNamespace(
                main_engine=main_engine,
                event_engine=event_engine,
                event_bridge=None,
                diagnostics={},
                close=lambda: events.append(("runtime_close",)),
            ),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            app = create_app(static_dir=Path(temp_dir))
            with TestClient(app):
                pass

        self.assertEqual(events, [
            ("init", True, False, True),
            ("bind_vnpy", main_engine, event_engine),
            ("reconcile", False, False),
            ("stop",),
            ("runtime_close",),
        ])
        self.assertIsNone(os.getenv(RUNTIME_SCHEDULER_DISABLE_DAILY_ENV))

    def test_lifespan_cleans_runtime_when_later_startup_initialization_fails(self) -> None:
        from api.app import create_app

        events = []
        main_engine = object()
        event_engine = object()

        class FakeRuntimeSchedulerService:
            def __init__(self, **_kwargs):
                events.append(("init",))

            def reconcile_from_config(self, **_kwargs):
                events.append(("reconcile",))

            def set_vnpy_runtime_engines(self, **_kwargs):
                events.append(("bind_vnpy",))

            def stop(self):
                events.append(("stop",))

        class FailingSystemConfigService:
            def __init__(self, **_kwargs):
                events.append(("system_config_failed",))
                raise RuntimeError("startup initialization failed")

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "src.config.get_config",
            return_value=SimpleNamespace(schedule_run_immediately=False),
        ), patch(
            "api.app.RuntimeSchedulerService",
            FakeRuntimeSchedulerService,
        ), patch(
            "api.app.SystemConfigService",
            FailingSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=SimpleNamespace(
                main_engine=main_engine,
                event_engine=event_engine,
                event_bridge=None,
                diagnostics={},
                close=lambda: events.append(("runtime_close",)),
            ),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            app = create_app(static_dir=Path(temp_dir))
            with self.assertRaisesRegex(RuntimeError, "startup initialization failed"):
                with TestClient(app):
                    pass

        self.assertEqual(events, [
            ("init",),
            ("bind_vnpy",),
            ("reconcile",),
            ("system_config_failed",),
            ("stop",),
            ("runtime_close",),
        ])

    def test_lifespan_suppresses_initial_start_without_losing_runtime_ownership(self) -> None:
        from api.app import create_app

        events = []

        class FakeRuntimeSchedulerService:
            def __init__(
                self,
                *,
                owns_schedule=True,
                force_enabled=False,
                daily_schedule_disabled=False,
                run_immediately_in_background=False,
                schedule_args_overrides=None,
                task_event_repository=None,
            ):
                events.append(("init", owns_schedule, force_enabled, run_immediately_in_background))

            def reconcile_from_config(self, *, run_immediately=False, clear_enabled_override=False):
                events.append(("reconcile", run_immediately, clear_enabled_override))

            def stop(self):
                events.append(("stop",))

        class FakeSystemConfigService:
            def __init__(self, runtime_scheduler=None):
                self.runtime_scheduler = runtime_scheduler

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {RUNTIME_SCHEDULER_SUPPRESS_START_ENV: "true"},
            clear=False,
        ), patch(
            "src.config.get_config",
            return_value=SimpleNamespace(schedule_run_immediately=True),
        ), patch("api.app.RuntimeSchedulerService", FakeRuntimeSchedulerService), patch(
            "api.app.SystemConfigService",
            FakeSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=_disabled_vnpy_runtime_handle(),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            app = create_app(static_dir=Path(temp_dir))
            with TestClient(app):
                pass

        self.assertEqual(events, [
            ("init", True, False, True),
            ("stop",),
        ])
        self.assertIsNone(os.getenv(RUNTIME_SCHEDULER_SUPPRESS_START_ENV))

    def test_lifespan_passes_runtime_scheduler_args_overrides(self) -> None:
        from api.app import create_app

        events = []
        runtime_args = {
            "no_notify": True,
            "no_market_review": True,
            "dry_run": True,
            "force_run": True,
            "single_notify": True,
            "no_context_snapshot": True,
            "workers": 4,
        }

        class FakeRuntimeSchedulerService:
            def __init__(
                self,
                *,
                owns_schedule=True,
                force_enabled=False,
                daily_schedule_disabled=False,
                run_immediately_in_background=False,
                schedule_args_overrides=None,
                task_event_repository=None,
            ):
                events.append(("init_args", schedule_args_overrides))

            def reconcile_from_config(self, *, run_immediately=False, clear_enabled_override=False):
                events.append(("reconcile", run_immediately, clear_enabled_override))

            def stop(self):
                events.append(("stop",))

        class FakeSystemConfigService:
            def __init__(self, runtime_scheduler=None):
                self.runtime_scheduler = runtime_scheduler

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {RUNTIME_SCHEDULER_ARGS_ENV: json.dumps(runtime_args)},
            clear=False,
        ), patch(
            "src.config.get_config",
            return_value=SimpleNamespace(schedule_run_immediately=True),
        ), patch("api.app.RuntimeSchedulerService", FakeRuntimeSchedulerService), patch(
            "api.app.SystemConfigService",
            FakeSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=_disabled_vnpy_runtime_handle(),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            app = create_app(static_dir=Path(temp_dir))
            with TestClient(app):
                pass

        self.assertEqual(events[0], ("init_args", runtime_args))
        self.assertIsNone(os.getenv(RUNTIME_SCHEDULER_ARGS_ENV))

    def test_lifespan_uses_configured_run_immediately_without_override(self) -> None:
        from api.app import create_app

        events = []

        class FakeRuntimeSchedulerService:
            def __init__(
                self,
                *,
                owns_schedule=True,
                force_enabled=False,
                daily_schedule_disabled=False,
                run_immediately_in_background=False,
                schedule_args_overrides=None,
                task_event_repository=None,
            ):
                events.append(("init", owns_schedule, force_enabled, run_immediately_in_background))

            def reconcile_from_config(self, *, run_immediately=False, clear_enabled_override=False):
                events.append(("reconcile", run_immediately, clear_enabled_override))

            def stop(self):
                events.append(("stop",))

        class FakeSystemConfigService:
            def __init__(self, runtime_scheduler=None):
                self.runtime_scheduler = runtime_scheduler

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=False), patch(
            "src.config.get_config",
            return_value=SimpleNamespace(schedule_run_immediately=True),
        ), patch("api.app.RuntimeSchedulerService", FakeRuntimeSchedulerService), patch(
            "api.app.SystemConfigService",
            FakeSystemConfigService,
        ), patch(
            "api.app.bootstrap_vnpy_runtime",
            return_value=_disabled_vnpy_runtime_handle(),
        ), patch("api.app._schedule_stock_index_background_refresh"):
            os.environ.pop(CLI_SCHEDULER_OWNER_ENV, None)
            os.environ.pop(RUNTIME_SCHEDULER_FORCE_ENABLED_ENV, None)
            os.environ.pop(RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV, None)
            os.environ.pop(RUNTIME_SCHEDULER_DISABLE_DAILY_ENV, None)

            app = create_app(static_dir=Path(temp_dir))
            with TestClient(app):
                pass

        self.assertEqual(events, [
            ("init", True, False, True),
            ("reconcile", True, False),
            ("stop",),
        ])


if __name__ == "__main__":
    unittest.main()
