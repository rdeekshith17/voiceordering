"""In-app agent scheduler: due jobs run once, failures are recorded and
don't stop other agents, and all seven agents are wired."""
from __future__ import annotations

from voiceorder.agents import scheduler
from voiceorder.agents.scheduler import Job, build_jobs, run_due_jobs
from voiceorder.tenants.store import TenantStore


def test_due_jobs_run_once_and_failures_are_isolated(tmp_path):
    store = TenantStore(tmp_path / "s.db")
    calls = []

    def boom():
        raise RuntimeError("POS down")

    jobs = [Job("fast", 60, lambda: calls.append("fast") or ["t1", "t2"]),
            Job("broken", 60, boom),
            Job("daily", 86400, lambda: calls.append("daily") or {"ok": True})]
    assert run_due_jobs(store, jobs, now=1_000_000) == ["fast", "broken", "daily"]
    assert run_due_jobs(store, jobs, now=1_000_030) == []  # nothing due 30 s later
    status = {s["job"]: s for s in store.job_status()}
    assert status["fast"]["last_status"] == "ok" and status["fast"]["last_detail"] == "2 item(s)"
    assert status["broken"]["last_status"] == "error" and "POS down" in status["broken"]["last_detail"]
    # finish_job stamps real time, so look a day ahead: only the 60 s jobs are due again
    later = status["fast"]["last_finished_at"] + 120
    assert run_due_jobs(store, jobs, now=later) == ["fast", "broken"]
    assert calls == ["fast", "daily", "fast"]


def test_all_seven_agents_are_scheduled_and_run_against_the_store(tmp_path):
    store = TenantStore(tmp_path / "a.db")
    store.create_tenant("Hyderabad House", "+15622680097")
    jobs = build_jobs(store, build_adapter=lambda t: (_ for _ in ()).throw(RuntimeError("no POS")),
                      get_catalog=lambda t: None, public_base_url="http://127.0.0.1:9")
    assert {j.name for j in jobs} == {"support_check", "fraud_watch", "menu_sync", "billing_rollup",
                                      "onboarding_check", "qa_review", "marketing_drafts"}
    run_due_jobs(store, jobs)
    status = {s["job"]: s["last_status"] for s in store.job_status()}
    assert len(status) == 7
    # These only read/write our own tables, so they must succeed on an empty restaurant.
    for name in ("fraud_watch", "billing_rollup", "onboarding_check", "qa_review"):
        assert status[name] == "ok", (name, store.job_status())


def test_scheduler_is_off_unless_enabled():
    import voiceorder.api.main as api_main
    assert not any(t.name == "agent-scheduler" for t in __import__("threading").enumerate())
    assert scheduler.MINUTE == 60 and api_main.tenant_store is not None
