"""
Local background jobs (jobs.run_detached's daemon-thread fallback and the
whatsapp first-sync thread) are capped by a module-level semaphore sized from
LOCAL_JOBS_MAX_CONCURRENT (default 4). Jobs past the cap QUEUE (their threads
block on the semaphore); nothing is ever dropped. Modal-dispatched jobs never
touch the cap.
"""
from __future__ import annotations

import threading
import time

import pytest

import backend.jobs as jobs

_RAN = threading.Event()


def _noop_job(db):
    """Top-level so run_detached can resolve it by dotted path."""
    _RAN.set()


@pytest.fixture
def cap2(monkeypatch):
    """Rebuild the semaphore with a cap of 2 for this test, then reset it so
    later tests rebuild from their own env."""
    monkeypatch.setenv("LOCAL_JOBS_MAX_CONCURRENT", "2")
    monkeypatch.setattr(jobs, "_local_jobs_sem", None)
    yield
    jobs._local_jobs_sem = None


def test_cap_limits_concurrency_and_queues_instead_of_dropping(cap2):
    lock = threading.Lock()
    running: list[int] = []
    done: list[int] = []
    peak = {"n": 0}
    release = threading.Event()

    def job(i):
        with lock:
            running.append(i)
            peak["n"] = max(peak["n"], len(running))
        release.wait(timeout=10)
        with lock:
            running.remove(i)
            done.append(i)

    threads = [threading.Thread(target=jobs._run_local_job,
                                args=(f"job-{i}", job, i), daemon=True)
               for i in range(3)]  # N+1 jobs for a cap of N=2
    for t in threads:
        t.start()

    # Exactly 2 jobs get slots; the third queues on the semaphore.
    deadline = time.time() + 5
    while time.time() < deadline:
        with lock:
            if len(running) == 2:
                break
        time.sleep(0.01)
    time.sleep(0.1)  # give the third job a chance to (wrongly) sneak in
    with lock:
        assert len(running) == 2
    assert peak["n"] == 2

    # Freeing the slots lets the queued job run: all 3 complete, none dropped.
    release.set()
    for t in threads:
        t.join(timeout=10)
    assert sorted(done) == [0, 1, 2]
    assert peak["n"] == 2  # the cap held for the whole run


def test_run_detached_local_fallback_routes_through_the_cap(monkeypatch):
    calls: list[str] = []
    orig = jobs._run_local_job

    def spy(job_name, target, *args, **kwargs):
        calls.append(job_name)
        orig(job_name, target, *args, **kwargs)

    monkeypatch.setattr(jobs, "_run_local_job", spy)
    monkeypatch.delenv("USE_MODAL", raising=False)
    _RAN.clear()

    assert jobs.run_detached(_noop_job) == "local"
    assert _RAN.wait(timeout=10), "detached job never ran"
    assert calls == ["detached-_noop_job"]


def test_semaphore_size_comes_from_env_with_default_4(monkeypatch):
    monkeypatch.setattr(jobs, "_local_jobs_sem", None)
    monkeypatch.delenv("LOCAL_JOBS_MAX_CONCURRENT", raising=False)
    sem = jobs._local_jobs_semaphore()
    assert sem._value == 4
    jobs._local_jobs_sem = None
    monkeypatch.setenv("LOCAL_JOBS_MAX_CONCURRENT", "1")
    assert jobs._local_jobs_semaphore()._value == 1
    jobs._local_jobs_sem = None
    monkeypatch.setenv("LOCAL_JOBS_MAX_CONCURRENT", "not-a-number")
    assert jobs._local_jobs_semaphore()._value == 4
    jobs._local_jobs_sem = None
