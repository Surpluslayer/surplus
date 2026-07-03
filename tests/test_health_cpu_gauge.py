"""Deep-health CPU gauge: cgroup delta math + the >=60% scaling warning.

CPU% cannot be read from a single cgroup sample (usage is cumulative), so the
endpoint diffs two deep calls. These tests feed synthetic /sys/fs/cgroup files
so the math is exercised on any OS (macOS/CI have no cgroup), and pin the two
properties that matter: the first call is null (no prior sample) and the second
computes usage over (wall * cores) and warns past the threshold.
"""
import builtins
import io
import os

import backend.main as m
from fastapi.testclient import TestClient


def _mk_open(state):
    real = builtins.open

    def fake(path, *a, **k):
        p = str(path)
        if p == "/sys/fs/cgroup/cpu.stat":
            return io.StringIO(f"usage_usec {state['usage_usec']}\n")
        if p == "/sys/fs/cgroup/cpu.max":
            return io.StringIO(state.get("cpu_max", "200000 100000"))  # 2 cores
        if p == "/sys/fs/cgroup/memory.current":
            return io.StringIO("100000000")
        if p == "/sys/fs/cgroup/memory.max":
            return io.StringIO("24000000000")
        return real(path, *a, **k)

    return fake, real


def _run(monkeypatch, usage_seq, times, cpu_max="200000 100000"):
    """Drive two deep calls; usage_seq/times are consumed across both."""
    os.environ["ADMIN_TOKEN"] = "testtok"
    state = {"usage_usec": usage_seq[0], "cpu_max": cpu_max}
    fake_open, real_open = _mk_open(state)
    tv = iter(times)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(m._time, "time", lambda: next(tv, times[-1]))
    m._CPU_SAMPLE.clear()
    h = {"X-Admin-Token": "testtok"}
    with TestClient(m.app) as c:
        r1 = c.get("/api/health?deep=1", headers=h).json()
        state["usage_usec"] = usage_seq[1]
        r2 = c.get("/api/health?deep=1", headers=h).json()
    return r1, r2


def test_first_call_null_second_call_computes(monkeypatch):
    # +10 CPU-seconds over 10s wall on 2 cores = 50%.
    r1, r2 = _run(monkeypatch, [0, 10_000_000], [1000.0, 1000.0, 1010.0, 1010.0])
    assert r1["cpu"]["used_pct"] is None          # no prior sample
    assert r1["cpu"]["cores"] == 2.0
    assert r2["cpu"]["used_pct"] == 50.0
    assert r2["cpu"]["window_s"] == 10.0
    assert not [w for w in r2["warnings"] if w.startswith("cpu")]  # 50 < 60


def test_sustained_high_cpu_trips_scaling_warning(monkeypatch):
    # +15 CPU-seconds over 10s on 2 cores = 75% -> warn.
    _r1, r2 = _run(monkeypatch, [0, 15_000_000], [1000.0, 1000.0, 1010.0, 1010.0])
    assert r2["cpu"]["used_pct"] == 75.0
    assert [w for w in r2["warnings"] if w.startswith("cpu ")]


def test_pct_clamped_and_unshaped_quota_falls_back(monkeypatch):
    # cpu.max "max" (no quota) -> cores from os.cpu_count(); a counter jump that
    # would exceed 100% is clamped, never a nonsense >100 reading.
    r1, r2 = _run(
        monkeypatch,
        [0, 10_000_000_000],                       # absurd jump
        [1000.0, 1000.0, 1001.0, 1001.0],
        cpu_max="max",
    )
    assert r2["cpu"]["used_pct"] == 100.0          # clamped, not >100
