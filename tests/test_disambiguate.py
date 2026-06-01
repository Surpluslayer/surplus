"""Tests for LLM company disambiguation (the Brittany/Kyndred same-name bug).

Contract:
  - needs_disambiguation() fires ONLY in the murky zone: >=2 candidates, no hard
    structural tie, a real claimed company, and the person's own profile hasn't
    already singled out exactly one candidate.
  - disambiguate_company() is fail-safe: with no API key / on any error / on an
    inconclusive verdict it returns without mutating candidates, so the existing
    deterministic ranking stands.
  - On a confident in-range pick it sets matches_llm_identity (+ reason) on the
    chosen candidate.
  - reconcile rewards matches_llm_identity (it breaks the same-name tie) but caps
    the resulting company-resolution confidence at "medium".
  - The flag survives the cache round-trip (as_dict/from_dict).
"""
from __future__ import annotations
from types import SimpleNamespace

import pytest

from backend.triage.answers import Claims
from backend.triage.enrich import CompanyCandidate, PersonEvidence, RawEvidence
from backend.triage import disambiguate as D
from backend.triage.disambiguate import needs_disambiguation, disambiguate_company
from backend.triage.reconcile import reconcile


def _applicant():
    return SimpleNamespace(id="a1", name="Brittany", email="brittany@kyndred.co",
                           linkedin_url="")


# Canonical shape: two same-named companies, neither carries a hard tie, the
# person's LinkedIn was stripped (no work/headline match on either).
def _kyndred_candidates():
    return [
        CompanyCandidate(name="Kyndred Health", source="linkedin_company",
                         website="https://kyndredhealth.com", industry="Hospitals",
                         follower_count=4200, description="National hospital network."),
        CompanyCandidate(name="Kyndred", source="linkedin_company",
                         website="", industry="Consumer Software",
                         follower_count=12,
                         description="Seed-stage app for matching friends."),
    ]


# ── Gate ─────────────────────────────────────────────────────────────────────

def test_gate_fires_on_ambiguous_same_name():
    claims = Claims(claimed_company="Kyndred", claimed_role="Co-Founder")
    person = PersonEvidence(found=True, headline="Building Kyndred")
    assert needs_disambiguation(claims, person, _kyndred_candidates()) is True


def test_gate_skips_when_hard_tie_present():
    """A name-on-page / domain match already resolves it — don't pay for an LLM."""
    claims = Claims(claimed_company="Kyndred", claimed_role="Co-Founder")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    cands[1].matches_person_name = True
    assert needs_disambiguation(claims, person, cands) is False


def test_gate_skips_single_candidate():
    claims = Claims(claimed_company="Kyndred")
    person = PersonEvidence(found=True)
    assert needs_disambiguation(claims, person, _kyndred_candidates()[:1]) is False


def test_gate_skips_without_claimed_company():
    claims = Claims(claimed_company="")
    person = PersonEvidence(found=True)
    assert needs_disambiguation(claims, person, _kyndred_candidates()) is False


def test_gate_skips_when_one_soft_tie_resolves_it():
    """If the person's own LinkedIn work history points at exactly one candidate,
    the deterministic ranker is fine — no LLM call."""
    claims = Claims(claimed_company="Kyndred")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    cands[1].matches_work_experience = True
    assert needs_disambiguation(claims, person, cands) is False


# ── Fail-safe behavior ───────────────────────────────────────────────────────

def test_disambiguate_noop_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    claims = Claims(claimed_company="Kyndred", claimed_role="Founder")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    assert disambiguate_company(claims, person, cands) is None
    assert not any(c.matches_llm_identity for c in cands)


def test_disambiguate_noop_when_gate_closed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    claims = Claims(claimed_company="")        # gate closed → no client call
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    assert disambiguate_company(claims, person, cands) is None


def test_disambiguate_swallows_client_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class BoomClient:
        class messages:
            @staticmethod
            def create(**_):
                raise RuntimeError("network down")

    claims = Claims(claimed_company="Kyndred", claimed_role="Founder")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    # Must not raise; returns None; leaves candidates untouched.
    assert disambiguate_company(claims, person, cands, client=BoomClient()) is None
    assert not any(c.matches_llm_identity for c in cands)


# ── Happy path (stubbed client) ──────────────────────────────────────────────

class _FakeClient:
    def __init__(self, payload: str):
        self._payload = payload
        self.messages = self._Messages(payload)

    class _Messages:
        def __init__(self, payload):
            self._payload = payload

        def create(self, **_):
            return SimpleNamespace(
                content=[SimpleNamespace(text=self._payload)])


def test_disambiguate_marks_chosen_candidate(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    claims = Claims(claimed_company="Kyndred", claimed_role="Co-Founder",
                    claimed_industry="consumer social")
    person = PersonEvidence(found=True, headline="Building Kyndred")
    cands = _kyndred_candidates()
    client = _FakeClient('{"choice": 1, "confidence": "high", '
                         '"reason": "consumer app matches claimed role"}')
    out = disambiguate_company(claims, person, cands, client=client)
    assert out["choice"] == 1
    assert cands[1].matches_llm_identity is True
    assert "consumer" in cands[1].llm_identity_reason
    assert cands[0].matches_llm_identity is False


def test_disambiguate_ignores_low_confidence(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    claims = Claims(claimed_company="Kyndred", claimed_role="Founder")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    client = _FakeClient('{"choice": 1, "confidence": "low", "reason": "unsure"}')
    disambiguate_company(claims, person, cands, client=client)
    assert not any(c.matches_llm_identity for c in cands)


def test_disambiguate_ignores_out_of_range(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    claims = Claims(claimed_company="Kyndred", claimed_role="Founder")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    client = _FakeClient('{"choice": 9, "confidence": "high", "reason": "x"}')
    disambiguate_company(claims, person, cands, client=client)
    assert not any(c.matches_llm_identity for c in cands)


def test_disambiguate_handles_no_match_minus_one(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    claims = Claims(claimed_company="Kyndred", claimed_role="Founder")
    person = PersonEvidence(found=True)
    cands = _kyndred_candidates()
    client = _FakeClient('{"choice": -1, "confidence": "high", "reason": "none fit"}')
    disambiguate_company(claims, person, cands, client=client)
    assert not any(c.matches_llm_identity for c in cands)


# ── Reconcile integration ────────────────────────────────────────────────────

def test_llm_identity_flips_the_pick_in_reconcile():
    """Without the flag the bigger namesake (website + 4k followers) wins; with the
    flag on the seed-stage company, reconcile selects it instead."""
    claims = Claims(claimed_company="Kyndred", claimed_role="Co-Founder")
    person = PersonEvidence(found=True, headline="Building Kyndred")

    # Baseline: deterministic ranker picks the bigger "Kyndred Health".
    cands = _kyndred_candidates()
    raw = RawEvidence(person=person, company_candidates=cands)
    packet = reconcile(_applicant(), claims, raw, triage_config={})
    assert packet.selected_company.name == "Kyndred Health"

    # With LLM identity on the real company, it now wins.
    cands2 = _kyndred_candidates()
    cands2[1].matches_llm_identity = True
    cands2[1].llm_identity_reason = "consumer app matches claimed founder role"
    raw2 = RawEvidence(person=person, company_candidates=cands2)
    packet2 = reconcile(_applicant(), claims, raw2, triage_config={})
    assert packet2.selected_company.name == "Kyndred"
    # Identity-only selection is never "high" — it's a reasoned inference.
    assert packet2.selected_company.confidence in ("medium", "low")
    assert "LLM identity match" in packet2.selected_company.reason


def test_llm_identity_survives_cache_roundtrip():
    c = CompanyCandidate(name="Kyndred", matches_llm_identity=True,
                         llm_identity_reason="consumer app")
    back = CompanyCandidate.from_dict(c.as_dict())
    assert back.matches_llm_identity is True
    assert back.llm_identity_reason == "consumer app"
