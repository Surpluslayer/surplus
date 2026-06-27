"""Relationship intelligence: spine, pipeline, and supporting agents.

Canonical imports (no flat shims):

  spine.relationships   — timeline, contact_summary, link_contact
  spine.memory          — ContactFact store
  spine.dedup           — duplicate contact merge
  pipeline.context      — gather, reconcile, summary
  pipeline.agent.run    — triage + Phase-2 agent
  pipeline.compose      — shared follow-up composer
  pipeline.send         — outbound sender + routing
  pipeline.proactive    — cadence + dated triggers
  observability         — deterministic-layer health snapshot
"""
