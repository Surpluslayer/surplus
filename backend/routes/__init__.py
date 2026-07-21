"""
HTTP routes : one router per surface.

The relationship side (book, relationships, inperson, followups, integrations,
messages, settings, accounts, teams, ...) plus the shared surfaces (auth,
billing, demo, webhooks, admin, privacy). See backend/main.py for the mounted
set.

The events-side routers (events, pipeline, matching, roi, triage, curation,
jobs) were retired and their modules deleted — see ARCHITECTURE.md.
"""
