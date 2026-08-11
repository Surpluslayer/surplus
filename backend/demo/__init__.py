"""backend/demo : the 80-lawyer / 30-day demo cohort generator.

Distinct from backend/demo_seed.py, which seeds the ONE fixed 3-person cast
for the public guided /demo walkthrough (routes/demo.py) and is untouched by
this package. This package builds a much larger, statistically-shaped cohort
for demonstrating the product at scale -- same production schema, same event
taxonomy, calibrated against what's actually real in this codebase (see
distributions.py for exactly what that is and isn't).

    provenance.py    -- tag/query helpers over models.DemoProvenance
    distributions.py -- the calibration layer: documented cadence/frequency
                         constants, honest about which are derived from real
                         in-code product design (sweep intervals, tiering)
                         versus reasonable documented assumptions (no live
                         telemetry was reachable when this was built -- see
                         the module docstring for why)
    cohort.py         -- the baseline generator: users, contacts,
                         interactions, drafts, sessions, over 30 days,
                         following real per-session behavioral sequences
    extended.py        -- the "beyond this" layer: signal x relationship
                         matching, jurisdiction-aware action outcomes (wired
                         to the real solicitation.py gate), outcome feedback
"""
