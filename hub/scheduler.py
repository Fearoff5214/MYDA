"""
Deferred work: timers and reminders.

Timers are in-memory -- if the hub restarts, a two-minute timer is gone, and
that is the right trade (nobody wants a reboot to fire yesterday's timer).
Reminders are persisted in SQLite and re-armed at startup, because "remind me
at six" must survive a restart. Phase 5 adds the reminder half.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("jarvis.scheduler")


class Scheduler:
    def __init__(self, announce: Callable[[str, str], Awaitable[None]]) -> None:
        self._sched = AsyncIOScheduler()
        self._announce = announce
        self._seq = 0

    def start(self) -> None:
        self._sched.start()
        log.info("scheduler started")

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def after(self, seconds: float, device: str, text: str, prefix: str = "timer") -> str:
        """Speak `text` on `device` after `seconds`."""
        job_id = self._next_id(prefix)
        run_at = datetime.now() + timedelta(seconds=seconds)
        self._sched.add_job(
            self._announce, "date", run_date=run_at,
            args=[device, text], id=job_id, misfire_grace_time=30,
        )
        log.info("scheduled %s at %s: %r", job_id, run_at.strftime("%H:%M:%S"), text)
        return job_id

    def at(self, when: datetime, device: str, text: str, prefix: str = "reminder") -> str:
        job_id = self._next_id(prefix)
        self._sched.add_job(
            self._announce, "date", run_date=when,
            args=[device, text], id=job_id, misfire_grace_time=300,
        )
        log.info("scheduled %s at %s: %r", job_id, when.isoformat(timespec="minutes"), text)
        return job_id

    def cancel(self, job_id: str) -> bool:
        try:
            self._sched.remove_job(job_id)
            return True
        except Exception:  # noqa: BLE001 - job already fired or never existed
            return False

    def cancel_all(self, prefix: str) -> int:
        jobs = [j for j in self._sched.get_jobs() if j.id.startswith(f"{prefix}-")]
        for job in jobs:
            job.remove()
        return len(jobs)

    def pending(self, prefix: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": j.id, "when": j.next_run_time, "text": j.args[1] if len(j.args) > 1 else ""}
            for j in self._sched.get_jobs()
            if prefix is None or j.id.startswith(f"{prefix}-")
        ]
