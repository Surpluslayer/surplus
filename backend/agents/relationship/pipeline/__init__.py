"""Relationship follow-up pipeline, organized by stage.

Stages (in order):
  1. spine     — durable reads (Contact, timeline, ContactFact)
  2. context   — gather, reconcile next_step, compress thread
  3. agent     — triage + Phase-2 decide (angle / skip / next_step)
  4. compose   — shared message composer (voice + thread → body)
  5. send      — route outbound + persist (clear fulfilled next_step)

Import paths at ``relationship.*`` remain as backward-compatible shims.
"""
