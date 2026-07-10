# -*- coding: utf-8 -*-
"""Runtime scheduler service for long-lived API/Web/Desktop processes."""

from __future__ import annotations

import logging
import os
import threading
import _thread
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Deque, Dict, List, Optional, Set

from src.config import Config, get_config
from src.scheduler import Scheduler, normalize_schedule_times

logger = logging.getLogger(__name__)
CLI_SCHEDULER_OWNER_ENV = "DSA_CLI_SCHEDULER_OWNS_SCHEDULE"
RUNTIME_SCHEDULER_FORCE_ENABLED_ENV = "DSA_RUNTIME_SCHEDULER_FORCE_ENABLED"
RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV = "DSA_RUNTIME_SCHEDULER_RUN_IMMEDIATELY"
RUNTIME_SCHEDULER_SUPPRESS_START_ENV = "DSA_RUNTIME_SCHEDULER_SUPPRESS_START"
RUNTIME_SCHEDULER_DISABLE_DAILY_ENV = "DSA_RUNTIME_SCHEDULER_DISABLE_DAILY"
RUNTIME_SCHEDULER_ARGS_ENV = "DSA_RUNTIME_SCHEDULER_ARGS"
RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS_ENV = "DSA_RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS"
RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS_ENV = "DSA_RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS"
_RUNTIME_ANALYSIS_LOCK = threading.Lock()
SCHEDULE_ARGS_OVERRIDE_KEYS = {
    "no_notify",
    "no_market_review",
    "dry_run",
    "force_run",
    "single_notify",
    "no_context_snapshot",
    "workers",
}


def run_with_global_analysis_lock(
    task_runner: Callable[[Config, Any, Optional[List[str]]], Any],
    config: Config,
    args: Any,
    stock_codes: Optional[List[str]] = None,
    *,
    blocking: bool = True,
) -> bool:
    """Execute a task while holding the shared runtime analysis lock."""
    if not _RUNTIME_ANALYSIS_LOCK.acquire(blocking=blocking):
        return False
    try:
        task_runner(config, args, stock_codes)
    finally:
        _RUNTIME_ANALYSIS_LOCK.release()
    return True


def _agent_event_monitor_interval_seconds(config: Config) -> int:
    """Return the validated Event Monitor polling interval in seconds."""
    interval_minutes = getattr(config, "agent_event_monitor_interval_minutes", 5)
    try:
        interval_minutes = max(1, int(interval_minutes))
    except (TypeError, ValueError):  # pragma: no cover - defensive branch
        logger.warning(
            "Invalid AGENT_EVENT_MONITOR_INTERVAL_MINUTES=%r; use fallback 5",
            interval_minutes,
        )
        interval_minutes = 5
    return interval_minutes * 60


def _parse_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; use fallback %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("Invalid %s=%r below minimum %s; use fallback %s", name, raw, minimum, default)
        return default
    if value > maximum:
        logger.warning("Invalid %s=%r above maximum %s; use fallback %s", name, raw, maximum, default)
        return default
    return value


def build_agent_event_monitor_background_tasks(
    config: Config,
    *,
    config_provider: Callable[[], Config],
) -> List[Dict[str, Any]]:
    """Build scheduler background tasks used by the runtime scheduler."""
    if not getattr(config, "agent_event_monitor_enabled", False):
        return []

    from src.services.alert_worker import AlertWorker

    interval_seconds = _agent_event_monitor_interval_seconds(config)
    try:
        alert_worker = AlertWorker(config_provider=config_provider)
    except Exception as exc:  # pragma: no cover - defensive branch
        logger.warning("Failed to initialize AlertWorker for event monitor: %s", exc)
        return []

    def event_monitor_task() -> Dict[str, Any]:
        stats = alert_worker.run_once()
        triggered_count = stats.get("triggered", 0)
        if triggered_count:
            logger.info("[EventMonitor] triggered %d alert(s)", triggered_count)
        return stats

    return [{
        "task": event_monitor_task,
        "interval_seconds": interval_seconds,
        "run_immediately": True,
        "name": "agent_event_monitor",
    }]


class RuntimeSchedulerService:
    """Manage scheduled analysis inside the current API/Web/Desktop process."""

    def __init__(
        self,
        *,
        config_provider: Callable[[], Config] = get_config,
        task_runner: Optional[Callable[[Config, Any, Optional[List[str]]], Any]] = None,
        owns_schedule: Optional[bool] = None,
        force_enabled: bool = False,
        daily_schedule_disabled: bool = False,
        run_immediately_in_background: bool = False,
        background_tasks_provider: Optional[Callable[[Config], List[Dict[str, Any]]]] = None,
        schedule_args_overrides: Optional[Dict[str, Any]] = None,
        task_event_repository: Optional[Any] = None,
        task_event_retention_days: Optional[int] = None,
        task_event_cleanup_interval_seconds: Optional[int] = None,
    ) -> None:
        self._config_provider = config_provider
        self._task_runner = task_runner
        if owns_schedule is None:
            owns_schedule = os.getenv(CLI_SCHEDULER_OWNER_ENV, "").strip().lower() not in {
                "1",
                "true",
                "yes",
                "on",
            }
        self._owns_schedule = owns_schedule
        self._force_enabled = force_enabled
        self._daily_schedule_disabled = daily_schedule_disabled
        self._run_immediately_in_background = run_immediately_in_background
        self._background_tasks_provider = background_tasks_provider
        self._schedule_args_overrides = {
            key: value
            for key, value in (schedule_args_overrides or {}).items()
            if key in SCHEDULE_ARGS_OVERRIDE_KEYS
        }
        self._background_task_cache: Dict[str, Dict[str, Any]] = {}
        self._background_task_wrappers: Dict[str, Dict[str, Any]] = {}
        self._background_task_events: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._task_event_repository = task_event_repository
        self._task_event_retention_days = (
            task_event_retention_days
            if task_event_retention_days is not None
            else _parse_int_env(
                RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS_ENV,
                30,
                minimum=0,
                maximum=3650,
            )
        )
        self._task_event_cleanup_interval_seconds = (
            task_event_cleanup_interval_seconds
            if task_event_cleanup_interval_seconds is not None
            else _parse_int_env(
                RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS_ENV,
                3600,
                minimum=0,
                maximum=86400,
            )
        )
        self._last_task_event_cleanup_at: Optional[datetime] = None
        self._background_task_registered_names: Set[str] = set()
        self._lock = threading.RLock()
        self._run_lock = _RUNTIME_ANALYSIS_LOCK
        self._scheduler: Optional[Scheduler] = None
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._last_run_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_skipped_at: Optional[str] = None
        self._last_skip_reason: Optional[str] = None

    def _make_schedule_args(self) -> SimpleNamespace:
        defaults = {
            "schedule": True,
            "no_run_immediately": True,
            "no_notify": False,
            "no_market_review": False,
            "dry_run": False,
            "force_run": False,
            "single_notify": False,
            "no_context_snapshot": False,
            "market_review": False,
            "serve": False,
            "serve_only": True,
            "stocks": None,
            "workers": None,
        }
        defaults.update(self._schedule_args_overrides)
        return SimpleNamespace(**defaults)

    def _reload_config(self) -> Config:
        from main import _reload_runtime_config

        return _reload_runtime_config()

    def _record_analysis_busy_skip(self) -> None:
        self._last_skipped_at = datetime.now().isoformat()
        self._last_skip_reason = "analysis_already_running"
        logger.warning("Runtime scheduler skipped run: analysis already running")

    def _run_analysis_locked(self, stock_codes: Optional[List[str]]) -> None:
        try:
            config = self._reload_config()
            runner = self._task_runner
            if runner is None:
                from main import run_scheduled_analysis

                runner = run_scheduled_analysis
            self._last_run_at = datetime.now().isoformat()
            result = runner(config, self._make_schedule_args(), stock_codes)
            if result is False:
                raise RuntimeError("runtime scheduled analysis reported failure")
            self._last_success_at = datetime.now().isoformat()
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - scheduled runs must not kill API process.
            self._last_error = str(exc)
            logger.exception("Runtime scheduled analysis failed: %s", exc)

    def _run_analysis_once(self, stock_codes: Optional[List[str]] = None) -> bool:
        if not self._run_lock.acquire(blocking=False):
            self._record_analysis_busy_skip()
            return False
        try:
            self._run_analysis_locked(stock_codes)
        finally:
            self._run_lock.release()
        return True

    def _current_times(self) -> List[str]:
        config = self._config_provider()
        return normalize_schedule_times(
            getattr(config, "schedule_times", None),
            fallback_time=getattr(config, "schedule_time", "18:00"),
        )

    def _is_schedule_enabled(self, config: Config) -> bool:
        if self._daily_schedule_disabled:
            return False
        return self._force_enabled or bool(getattr(config, "schedule_enabled", False))

    def _current_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        if self._background_tasks_provider is not None:
            return self._background_tasks_provider(config)
        tasks = self._current_agent_event_monitor_background_tasks(config)
        tasks.extend(self._current_vnpy_paper_trading_background_tasks(config))
        return tasks

    def _current_vnpy_paper_trading_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        try:
            from src.services.vnpy_paper_trading_service import build_vnpy_paper_trading_background_tasks

            return build_vnpy_paper_trading_background_tasks()
        except Exception as exc:  # pragma: no cover - defensive branch
            logger.warning("Failed to build vn.py paper trading background tasks: %s", exc)
            return []

    def _current_agent_event_monitor_background_tasks(self, config: Config) -> List[Dict[str, Any]]:
        name = "agent_event_monitor"
        if not getattr(config, "agent_event_monitor_enabled", False):
            self._background_task_cache.pop(name, None)
            self._background_task_registered_names.discard(name)
            return []

        cached = self._background_task_cache.get(name)
        if cached is None:
            entries = build_agent_event_monitor_background_tasks(
                config,
                config_provider=self._reload_config,
            )
            if not entries:
                self._background_task_cache.pop(name, None)
                self._background_task_registered_names.discard(name)
                return []
            cached = dict(entries[0])
            cached["name"] = name
            self._background_task_cache[name] = cached
            interval_seconds = int(cached["interval_seconds"])
        else:
            interval_seconds = _agent_event_monitor_interval_seconds(config)

        run_immediately = (
            bool(cached.get("run_immediately", False))
            and name not in self._background_task_registered_names
        )
        self._background_task_registered_names.add(name)
        return [{
            "task": cached["task"],
            "interval_seconds": interval_seconds,
            "run_immediately": run_immediately,
            "name": name,
        }]

    @staticmethod
    def _summarize_background_task_result(result: Any) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return {"result": str(result)[:300]} if result is not None else {}
        interesting_keys = {
            "accepted",
            "skipped",
            "reason",
            "agent_run_uid",
            "agentRunUid",
            "candidate_count",
            "candidateCount",
            "planned_count",
            "plannedCount",
            "submitted_count",
            "submittedCount",
            "skipped_count",
            "skippedCount",
            "attempted_count",
            "attemptedCount",
            "failed_count",
            "failedCount",
            "messages",
        }
        details = {
            key: value
            for key, value in result.items()
            if key in interesting_keys
        }
        messages = details.get("messages")
        if isinstance(messages, list) and len(messages) > 5:
            details["messages"] = messages[:5]
            details["message_count"] = len(messages)
        return details

    def _record_background_task_event(
        self,
        *,
        name: str,
        status: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        timestamp = datetime.now()
        event: Dict[str, Any] = {
            "name": name,
            "status": status,
            "message": message,
            "timestamp": timestamp.isoformat(),
            "details": details or {},
        }
        if duration_seconds is not None:
            event["duration_seconds"] = round(duration_seconds, 3)
        with self._lock:
            self._background_task_events.append(event)
        repository = self._task_event_repository
        if repository is not None:
            try:
                repository.record_task_event(
                    name=name,
                    status=status,
                    message=message,
                    details=details or {},
                    duration_seconds=event.get("duration_seconds"),
                    timestamp=timestamp,
                )
                self._cleanup_persisted_task_events_if_due(timestamp)
            except Exception as exc:  # pragma: no cover - defensive persistence fallback
                logger.warning("Failed to persist runtime scheduler task event: %s", exc)

    def _cleanup_persisted_task_events_if_due(self, now: datetime) -> None:
        repository = self._task_event_repository
        cleanup_task_events = getattr(repository, "cleanup_task_events", None)
        if repository is None or not callable(cleanup_task_events):
            return
        retention_days = self._task_event_retention_days
        if retention_days <= 0:
            return
        interval_seconds = max(0, int(self._task_event_cleanup_interval_seconds))
        last_cleanup = self._last_task_event_cleanup_at
        if (
            last_cleanup is not None
            and interval_seconds > 0
            and (now - last_cleanup).total_seconds() < interval_seconds
        ):
            return
        cutoff = now - timedelta(days=retention_days)
        try:
            deleted = int(cleanup_task_events(older_than=cutoff) or 0)
        except Exception as exc:  # pragma: no cover - cleanup should not break task execution
            logger.warning("Failed to cleanup persisted runtime scheduler task events: %s", exc)
            return
        self._last_task_event_cleanup_at = now
        if deleted:
            logger.info(
                "Cleaned up %d runtime scheduler task event(s) older than %s",
                deleted,
                cutoff.isoformat(),
            )

    def task_events(
        self,
        *,
        name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent background task events with optional filters."""
        try:
            safe_limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            safe_limit = 50
        name_filter = str(name or "").strip()
        status_filter = str(status or "").strip()
        repository = self._task_event_repository
        if repository is not None:
            try:
                return repository.list_task_events(
                    name=name_filter or None,
                    status=status_filter or None,
                    limit=safe_limit,
                )
            except Exception as exc:  # pragma: no cover - defensive read fallback
                logger.warning("Failed to read persisted runtime scheduler task events: %s", exc)
        with self._lock:
            events = list(self._background_task_events)
        if name_filter:
            events = [event for event in events if str(event.get("name") or "") == name_filter]
        if status_filter:
            events = [event for event in events if str(event.get("status") or "") == status_filter]
        return events[-safe_limit:]

    def _instrument_background_task(self, name: str, task: Callable[[], Any]) -> Callable[[], Any]:
        cached = self._background_task_wrappers.get(name)
        if cached is not None and cached.get("source") is task:
            return cached["wrapped"]

        def wrapped_task() -> Any:
            started_at = datetime.now()
            self._record_background_task_event(
                name=name,
                status="started",
                message=f"Background task started: {name}",
            )
            try:
                result = task()
            except Exception as exc:
                duration = (datetime.now() - started_at).total_seconds()
                self._record_background_task_event(
                    name=name,
                    status="failed",
                    message=str(exc) or f"Background task failed: {name}",
                    details={
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    },
                    duration_seconds=duration,
                )
                raise
            duration = (datetime.now() - started_at).total_seconds()
            details = self._summarize_background_task_result(result)
            skipped = details.get("skipped") is True
            reason = str(details.get("reason") or "").strip()
            status = "skipped" if skipped else "completed"
            message = (
                f"Background task skipped: {reason}"
                if skipped and reason
                else f"Background task {status}: {name}"
            )
            self._record_background_task_event(
                name=name,
                status=status,
                message=message,
                details=details,
                duration_seconds=duration,
            )
            return result

        self._background_task_wrappers[name] = {
            "source": task,
            "wrapped": wrapped_task,
        }
        return wrapped_task

    @staticmethod
    def _run_in_background_thread(target: Callable[[], None]) -> None:
        """Run a callback in a background thread without blocking startup."""
        try:
            _thread.start_new_thread(target, ())
            return
        except Exception:
            # Best-effort fallback for environments where the low-level thread API
            # is unavailable or restricted.
            thread = threading.Thread(target=target, daemon=True)
            thread.start()

    def start(self, *, run_immediately: bool = False) -> None:
        with self._lock:
            if not self._owns_schedule:
                self.stop()
                return
            config = self._config_provider()
            daily_enabled = self._is_schedule_enabled(config)
            background_tasks = self._current_background_tasks(config)
            if not daily_enabled and not background_tasks:
                self.stop()
                return
            self.stop()
            times = normalize_schedule_times(
                getattr(config, "schedule_times", None),
                fallback_time=getattr(config, "schedule_time", "18:00"),
            )
            scheduler = Scheduler(
                schedule_time=getattr(config, "schedule_time", "18:00"),
                schedule_times=times,
                schedule_times_provider=self._current_times,
                register_signals=False,
            )
            if daily_enabled:
                if run_immediately and self._run_immediately_in_background:
                    scheduler.set_daily_task(self._run_analysis_once, run_immediately=False)
                else:
                    scheduler.set_daily_task(self._run_analysis_once, run_immediately=run_immediately)
            for entry in background_tasks:
                task_name = str(entry.get("name") or getattr(entry["task"], "__name__", "background_task"))
                task_kwargs = {
                    "task": self._instrument_background_task(task_name, entry["task"]),
                    "interval_seconds": entry["interval_seconds"],
                    "run_immediately": entry.get("run_immediately", False),
                    "name": task_name,
                }
                if entry.get("initial_delay_seconds") is not None:
                    task_kwargs["initial_delay_seconds"] = entry.get("initial_delay_seconds")
                scheduler.add_background_task(**task_kwargs)
            if daily_enabled and run_immediately and self._run_immediately_in_background:
                self._run_in_background_thread(self._run_analysis_once)
            thread = threading.Thread(
                target=scheduler.run,
                daemon=True,
                name="runtime-scheduler",
            )
            self._scheduler = scheduler
            self._thread = thread
            self._enabled = True
            thread.start()

    def stop(self) -> None:
        scheduler = self._scheduler
        if scheduler is not None:
            scheduler.stop()
        self._scheduler = None
        self._thread = None
        self._enabled = False

    def reconcile_from_config(
        self,
        *,
        run_immediately: bool = False,
        clear_enabled_override: bool = False,
    ) -> None:
        if clear_enabled_override:
            self._force_enabled = False
        if not self._owns_schedule:
            self.stop()
            return
        self.start(run_immediately=run_immediately)

    def run_now(self) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            self._record_analysis_busy_skip()
            return {
                "accepted": False,
                "running": True,
                "reason": "analysis_already_running",
            }

        def run_and_release() -> None:
            try:
                self._run_analysis_locked(None)
            finally:
                self._run_lock.release()

        worker = threading.Thread(
            target=run_and_release,
            daemon=True,
            name="runtime-scheduler-run-now",
        )
        try:
            worker.start()
        except Exception:
            self._run_lock.release()
            raise
        return {"accepted": True, "running": True}

    def status(self) -> Dict[str, Any]:
        scheduler = self._scheduler
        jobs = scheduler.schedule.get_jobs() if scheduler is not None else []
        next_run_candidates: List[datetime] = []
        if jobs:
            next_run_candidates.append(min(job.next_run for job in jobs))
        background_tasks: List[Dict[str, Any]] = []
        if scheduler is not None:
            raw_tasks = getattr(scheduler, "background_tasks", None)
            if raw_tasks is None:
                raw_tasks = getattr(scheduler, "_background_tasks", [])
            for entry in raw_tasks or []:
                worker = entry.get("thread") if isinstance(entry, dict) else None
                worker_running = bool(worker is not None and hasattr(worker, "is_alive") and worker.is_alive())
                next_run_at = None
                if isinstance(entry, dict):
                    try:
                        last_run = entry.get("last_run")
                        interval_seconds = entry.get("interval_seconds")
                        if last_run is not None and interval_seconds is not None:
                            next_run_dt = datetime.fromtimestamp(float(last_run) + float(interval_seconds))
                            next_run_at = next_run_dt.isoformat()
                            next_run_candidates.append(next_run_dt)
                    except (TypeError, ValueError, OSError, OverflowError):
                        next_run_at = None
                background_tasks.append({
                    "name": str(entry.get("name") or "background_task") if isinstance(entry, dict) else "background_task",
                    "interval_seconds": entry.get("interval_seconds") if isinstance(entry, dict) else None,
                    "initial_delay_seconds": entry.get("initial_delay_seconds") if isinstance(entry, dict) else None,
                    "running": bool(entry.get("running", False)) or worker_running if isinstance(entry, dict) else False,
                    "last_run": entry.get("last_run") if isinstance(entry, dict) else None,
                    "next_run_at": next_run_at,
                })
        task_events = list(self._background_task_events)[-20:]
        next_run = min(next_run_candidates).isoformat() if next_run_candidates else None
        if scheduler is not None:
            schedule_times = list(getattr(scheduler, "schedule_times", []))
        else:
            try:
                schedule_times = self._current_times()
            except Exception:  # pragma: no cover - defensive status fallback
                schedule_times = []
        thread = self._thread
        loop_running = bool(thread is not None and thread.is_alive())
        running = self._run_lock.locked()
        return {
            "enabled": self._enabled,
            "running": running,
            "loop_running": loop_running,
            "schedule_times": schedule_times,
            "next_run_at": next_run,
            "last_run_at": self._last_run_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_skipped_at": self._last_skipped_at,
            "last_skip_reason": self._last_skip_reason,
            "background_tasks": background_tasks,
            "task_events": task_events,
        }
