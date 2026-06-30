"""Stage 5: outbound send + logging."""
from .flow import route_and_send
from .sender import (
    automated_send_enabled,
    automated_sends_enabled,
    fire_booking_on_send,
    send_and_log,
    send_followup_email,
)
