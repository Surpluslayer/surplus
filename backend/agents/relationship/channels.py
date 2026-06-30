"""agents/relationship/channels.py : single source of truth for messaging channels.

One place owns "what channels exist" and how a stored RelationshipInteraction.source_type
maps to a channel. Before this, the same sets were duplicated across behavioral.py,
gather.py, spine/relationships.py and routes/messages.py -- and a channel missing from one
of them silently dropped ingested iMessage/SMS out of the context the drafter reads.
"""
from __future__ import annotations

# Channels a back-and-forth conversation can happen on.
MESSAGING_CHANNELS = frozenset({"linkedin", "email", "whatsapp", "imessage", "sms"})

# Of those, the ones that send through a user DEVICE (companion), not the cloud.
DEVICE_CHANNELS = frozenset({"imessage", "sms"})

# source_type values that count as conversational rows in a thread: the messaging
# channels plus the legacy capture / note / outreach types.
MESSAGE_SOURCE_TYPES = MESSAGING_CHANNELS | {
    "in_person_capture", "manual_note", "linkedin_outreach"}

# Map a stored source_type -> its channel (messaging channels map to themselves).
CHANNEL_BY_SOURCE = {
    "manual_note": "manual",
    "email_interaction": "email",
    "calendar_meeting": "calendar",
    "relationship_interaction": "manual",
    "draft_generated": "manual",
    **{c: c for c in MESSAGING_CHANNELS},
}
