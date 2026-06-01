"""
routes/draft.py : the intent-steered "connect" drafting endpoint.

The front end's "Preview note + follow-up" step POSTs
    { contact, sender, intent, context }
and gets back
    { connection_note, first_message }

Intent (a preset label OR the user's freeform "other" text) is the strongest
lever : Sales / Hiring / Networking / Vibes / freeform each produce a genuinely
different draft. See agents/draft.py for the prompt.

Signed-in only. The sender's saved booking link (User.calendly_url) is woven into
the first message as the next step unless the caller passes its own booking_link.
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..agents.draft import draft_connect
from ..auth import current_user
from ..models import User

router = APIRouter(prefix="/api", tags=["draft"])


class Contact(BaseModel):
    name: str
    headline: str = ""


class Sender(BaseModel):
    name: str = ""
    role: str = ""
    company: str = ""
    # Optional per-request override; defaults to the signed-in user's saved link.
    booking_link: Optional[str] = None


class DraftRequest(BaseModel):
    contact: Contact
    sender: Sender = Sender()
    intent: str                 # preset label OR the user's freeform "other" text
    context: str = ""           # what they talked about


@router.post("/draft")
def draft(req: DraftRequest, user: User = Depends(current_user)) -> dict[str, str]:
    # Fill sender identity from the signed-in user when the client omits it, and
    # default the booking link to their saved Calendly (set once in Quick setup).
    booking = req.sender.booking_link or user.calendly_url
    return draft_connect(
        contact_name=req.contact.name,
        contact_headline=req.contact.headline,
        sender_name=req.sender.name or user.name or "",
        sender_role=req.sender.role,
        sender_company=req.sender.company,
        intent=req.intent,
        context=req.context,
        booking_link=booking,
    )
