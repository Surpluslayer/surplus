"""Outlook-through-Unipile: the /email/start provider filter.

Outlook connects via the SAME Unipile hosted-auth flow as Gmail (no separate
Microsoft/Azure app). A ?provider hint narrows the hosted link to one provider
so the UI can offer a one-tap "Connect Outlook" button; omitted, both are shown.
Unipile's token for Microsoft mail is OUTLOOK (MICROSOFT is rejected), so the
mapping must emit OUTLOOK.
"""
from backend.routes import auth as auth_routes


def test_provider_hint_maps_to_unipile_tokens():
    f = auth_routes._email_providers_for
    assert f("outlook") == ["OUTLOOK"]
    assert f("microsoft") == ["OUTLOOK"]      # UI may say microsoft; Unipile wants OUTLOOK
    assert f("m365") == ["OUTLOOK"]
    assert f("google") == ["GOOGLE"]
    assert f("gmail") == ["GOOGLE"]
    assert f("") == ["GOOGLE", "OUTLOOK"]     # generic button: both, Unipile picker
    assert f("banana") == ["GOOGLE", "OUTLOOK"]
    assert f("OUTLOOK") == ["OUTLOOK"]        # case-insensitive


def test_create_body_honors_single_provider():
    body = auth_routes._email_create_body(
        "https://dsn", "2026-01-01T00:00:00Z", "state123",
        "https://event.surpluslayer.com", "https://fail",
        providers=["OUTLOOK"])
    assert body["providers"] == ["OUTLOOK"]
    assert body["name"] == "state123"
    # webhook/callback wiring unchanged for the single-provider path
    assert body["notify_url"].endswith("/api/auth/email/webhook")
    assert "state=state123" in body["success_redirect_url"]


def test_create_body_defaults_to_both():
    body = auth_routes._email_create_body(
        "https://dsn", "2026-01-01T00:00:00Z", "s", "https://b", "https://f")
    assert body["providers"] == ["GOOGLE", "OUTLOOK"]
