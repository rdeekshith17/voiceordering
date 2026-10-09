"""Runs the background agents inside the web app (AGENTS_ENABLED=1).

A daemon thread wakes every minute and starts each agent that is due. Due-ness
and exclusivity come from the job_runs lease in the database, so restarts
don't reset schedules and two app instances never run the same agent at once.
While the host sleeps (Render free plan) nothing runs; overdue agents run on
the next wake-up. Every agent is idempotent or deduplicated, so a late or
repeated run is harmless.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..tenants.store import TenantStore

log = logging.getLogger("voiceorder.agents")

MINUTE, HOUR, DAY = 60, 3600, 86400


@dataclass(frozen=True)
class Job:
    name: str
    every: float  # seconds between runs
    run: Callable[[], Any]
    lease: float = 30 * MINUTE  # a run that outlives this is presumed dead


def build_jobs(store: TenantStore, build_adapter: Callable, get_catalog: Callable,
               public_base_url: str | None) -> list[Job]:
    """The seven agents with the cadence each one documents."""
    from .billing import rollup_day
    from .fraud import run_fraud_watch
    from .marketing import run_marketing
    from .menusync import run_menu_sync
    from .onboarding import run_onboarding_check
    from .qa import run_qa_review
    from .support import run_support_check

    def pos_ok(tenant) -> bool:
        try:
            build_adapter(tenant).ping()
            return True
        except Exception:
            return False

    return [
        Job("support_check", 15 * MINUTE,
            lambda: run_support_check(store, build_adapter=build_adapter,
                                      public_base_url=public_base_url)),
        Job("fraud_watch", HOUR, lambda: run_fraud_watch(store)),
        Job("menu_sync", 6 * HOUR, lambda: run_menu_sync(store, check_pos=pos_ok)),
        Job("billing_rollup", DAY, lambda: rollup_day(store)),
        Job("onboarding_check", DAY, lambda: run_onboarding_check(store)),
        Job("qa_review", DAY, lambda: run_qa_review(store)),
        Job("marketing_drafts", 7 * DAY, lambda: run_marketing(store, get_catalog=get_catalog)),
    ]


def _summary(result: Any) -> str:
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    try:
        return json.dumps(result, default=str)[:300]
    except (TypeError, ValueError):
        return str(result)[:300]


def run_due_jobs(store: TenantStore, jobs: list[Job], now: float | None = None) -> list[str]:
    """Start every job that is due and unclaimed. Returns the names that ran."""
    ran = []
    for job in jobs:
        if not store.try_start_job(job.name, job.lease, job.every, now=now):
            continue
        started = time.monotonic()
        try:
            detail = _summary(job.run())
            store.finish_job(job.name, "ok", detail)
            log.info("agent %s ok in %.1fs: %s", job.name, time.monotonic() - started, detail)
        except Exception as exc:  # one failing agent never stops the others
            store.finish_job(job.name, "error", f"{type(exc).__name__}: {exc}")
            log.exception("agent %s failed", job.name)
        ran.append(job.name)
    return ran


def start(store: TenantStore, jobs: list[Job], first_delay: float = 30,
          tick: float = MINUTE) -> threading.Thread:
    """Start the scheduler thread (daemon: it dies with the app)."""
    def loop() -> None:
        time.sleep(first_delay)  # let the app finish starting and answer calls first
        while True:
            try:
                run_due_jobs(store, jobs)
            except Exception:
                log.exception("agent scheduler tick failed")
            time.sleep(tick)

    thread = threading.Thread(target=loop, name="agent-scheduler", daemon=True)
    thread.start()
    log.info("agent scheduler started: %s", ", ".join(j.name for j in jobs))
    return thread
