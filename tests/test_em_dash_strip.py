"""
Outbound copy must never contain em/en dashes : they read machine-written in a
LinkedIn note. The guarantee lives in LeadPayload.__post_init__, so EVERY send
path (invite note, post-accept DM, follow-up, AI auto-reply) is covered because
they all build a LeadPayload before reaching a provider.

We test the pure helper plus the load-bearing LeadPayload guard.
"""
from __future__ import annotations

from backend.providers.base import LeadPayload, strip_em_dashes


# ── the pure sanitizer ──────────────────────────────────────────────────

def test_spaced_em_dash_becomes_comma():
    assert strip_em_dashes("Loved your post — would be great to connect") == \
        "Loved your post, would be great to connect"


def test_tight_em_dash_becomes_comma():
    assert strip_em_dashes("infra—at scale") == "infra, at scale"


def test_en_dash_and_figure_dash_and_minus_all_stripped():
    for d in ("–", "―", "−"):  # en dash, horizontal bar, minus
        out = strip_em_dashes(f"a {d} b")
        assert "—" not in out and d not in out
        assert out == "a, b"


def test_ascii_hyphen_is_preserved():
    # co-founder, follow-up, 10-person : real hyphens must survive.
    s = "co-founder of a 10-person follow-up team"
    assert strip_em_dashes(s) == s


def test_trailing_dash_does_not_leave_dangling_comma():
    assert strip_em_dashes("Talk soon —") == "Talk soon"


def test_empty_and_none_pass_through():
    assert strip_em_dashes("") == ""
    assert strip_em_dashes(None) is None


def test_no_dashes_is_identity():
    s = "Hey Maya, loved your work on the platform team."
    assert strip_em_dashes(s) == s


# ── the load-bearing guard : every send builds a LeadPayload ─────────────

def _lead(note: str, message: str) -> LeadPayload:
    return LeadPayload(
        event_id=1, prospect_id=1, identity="x", first_name="A", last_name="B",
        full_name="A B", linkedin_url="https://www.linkedin.com/in/x",
        company="Acme", position="Eng", note=note, message=message,
    )


def test_lead_payload_strips_note_and_message():
    lead = _lead(
        note="Hi — saw your post",
        message="Great to connect — I lead infra—platform here",
    )
    assert "—" not in lead.note
    assert "—" not in lead.message
    assert lead.note == "Hi, saw your post"
    assert lead.message == "Great to connect, I lead infra, platform here"


def test_lead_payload_leaves_clean_text_untouched():
    lead = _lead(note="Hi Maya, quick hello.",
                 message="Loved your co-founder story.")
    assert lead.note == "Hi Maya, quick hello."
    assert lead.message == "Loved your co-founder story."
