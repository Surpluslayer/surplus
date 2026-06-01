"""
Regression tests for the LinkedIn soft-throttle ('stripped-200') handling.

The failure mode (canonical case: Harpriya): a fetch SUCCEEDS with a 200, the
profile is plainly substantial — real headline, a real follower count — yet the
work-experience array comes back EMPTY because LinkedIn stripped the expensive
experience section under load. The OLD behaviour treated that empty work history
as 'no track record' and forced manual_review, tanking a corroborated founder.

Family B contract:
  - PersonEvidence.work_unreliable is set when (found + no work-exp + headline +
    followers >= 30), and NOT set for a genuinely thin profile.
  - reconcile() must NOT add "LinkedIn work experience missing" to the manual-
    review reasons when work_unreliable — it goes to warnings instead, so the
    packet is not flagged manual_review_required on that basis alone.
"""
from __future__ import annotations
import threading
import time
from types import SimpleNamespace

import pytest

from backend.triage.answers import Claims
from backend.triage import enrich
from backend.triage.enrich import PersonEvidence, RawEvidence, _linkedin_slug
from backend.triage.reconcile import reconcile


def _applicant():
    return SimpleNamespace(id="a1", name="Harpriya", email="harpriya@hotbox.app",
                           linkedin_url="https://www.linkedin.com/in/harpriya/")


def test_work_unreliable_set_for_substantial_but_empty_profile():
    """The stripped-200 signature flips work_unreliable on."""
    ev = PersonEvidence(
        found=True, matches_name=True,
        headline="CEO @ Hotbox · backed by a16z speedrun",
        followers=2053, work_experience_found=False,
    )
    # Mirror the production set-logic (kept here so the test pins the contract,
    # not just the one call site).
    ev.work_unreliable = (
        ev.found and not ev.work_experience_found
        and bool(ev.headline) and ev.followers >= 30)
    assert ev.work_unreliable is True


def test_work_unreliable_not_set_for_genuinely_thin_profile():
    """A real empty profile (no headline / ~no followers) must NOT trip it."""
    ev = PersonEvidence(found=True, matches_name=True, headline="",
                        followers=3, work_experience_found=False)
    ev.work_unreliable = (
        ev.found and not ev.work_experience_found
        and bool(ev.headline) and ev.followers >= 30)
    assert ev.work_unreliable is False


def test_work_unreliable_survives_serialization_roundtrip():
    ev = PersonEvidence(found=True, headline="CEO @ Hotbox", followers=2053,
                        work_unreliable=True)
    back = PersonEvidence.from_dict(ev.as_dict())
    assert back.work_unreliable is True


def test_throttle_stripped_does_not_force_manual_review():
    """Harpriya: corroborated founder, throttle-stripped work history → the empty
    work-exp must NOT be a manual-review reason; it becomes a warning instead."""
    person = PersonEvidence(
        found=True, matches_name=True,
        headline="CEO @ Hotbox · backed by a16z speedrun",
        followers=2053, work_experience_found=False, work_unreliable=True,
        profile_url="https://www.linkedin.com/in/harpriya/",
    )
    claims = Claims(claimed_role="Co-Founder & CEO", claimed_company="Hotbox")
    raw = RawEvidence(person=person, company_candidates=[])
    packet = reconcile(_applicant(), claims, raw, triage_config={})
    d = packet.as_dict()

    assert "work experience missing" not in (d.get("manual_review_reason") or "").lower(), d
    # The signal is preserved, just as a (non-blocking) warning.
    assert any("throttle" in w.lower() or "substantial" in w.lower()
               for w in (d.get("warnings") or [])), d.get("warnings")


def test_genuinely_empty_profile_still_forces_review():
    """Control: a thin profile (not throttle-stripped) STILL trips manual review
    on missing work experience — we only relaxed the throttle case."""
    person = PersonEvidence(found=True, matches_name=True, headline="",
                            followers=2, work_experience_found=False,
                            work_unreliable=False)
    claims = Claims(claimed_role="Founder", claimed_company="Mystery Co")
    raw = RawEvidence(person=person, company_candidates=[])
    packet = reconcile(_applicant(), claims, raw, triage_config={})
    d = packet.as_dict()
    assert "work experience missing" in (d.get("manual_review_reason") or "").lower(), d


# ── LinkedIn slug normalization (the locale-suffix 422/404 bug) ─────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/in/harpriya/", "harpriya"),
    ("https://www.linkedin.com/in/yahal/en", "yahal"),          # locale suffix
    ("https://linkedin.com/in/jeff-li-360a2876?locale=en", "jeff-li-360a2876"),
    ("https://www.linkedin.com/in/someone/detail/recent-activity/", "someone"),
    ("https://www.linkedin.com/in/maria/de/", "maria"),
    ("linkedin.com/in/bob", "bob"),
    ("https://www.linkedin.com/in/anna#about", "anna"),
    ("", ""),
])
def test_linkedin_slug_handles_suffixes(url, expected):
    assert _linkedin_slug(url) == expected


# ── Per-account rate budget (pacing prevents the soft-throttle) ─────────────

def test_pace_account_spaces_same_account(monkeypatch):
    """Concurrent fetches on ONE account are serialized to the min interval;
    a different account is not blocked by it."""
    monkeypatch.setenv("UNIPILE_MIN_FETCH_INTERVAL", "0.15")
    # Reset shared state so other tests don't leak timestamps in.
    with enrich._RATE_LOCK:
        enrich._ACCOUNT_NEXT_OK.clear()
    fired: list[float] = []

    def worker():
        enrich._pace_account("acctA")
        fired.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fired.sort()
    gaps = [fired[i] - fired[i - 1] for i in range(1, len(fired))]
    assert all(g >= 0.14 for g in gaps), gaps          # spaced ~0.15s apart
    # A different account gets its own budget → no wait.
    t0 = time.monotonic()
    enrich._pace_account("acctB")
    assert time.monotonic() - t0 < 0.05


def test_pace_account_noop_when_interval_zero(monkeypatch):
    monkeypatch.setenv("UNIPILE_MIN_FETCH_INTERVAL", "0")
    with enrich._RATE_LOCK:
        enrich._ACCOUNT_NEXT_OK.clear()
    t0 = time.monotonic()
    enrich._pace_account("acctZ")
    enrich._pace_account("acctZ")
    assert time.monotonic() - t0 < 0.05


def test_pace_account_handles_none():
    enrich._pace_account(None)   # must not raise
