"""Thin wrapper around Twilio's REST API for sending WhatsApp Content
Template messages (special-offer broadcasts).

This is the first code path in the app that calls Twilio's API directly —
everything else so far (app/webhook.py's /twilio/whatsapp route) only
*replies* to inbound messages via TwiML.
"""
from __future__ import annotations

import json
import logging

from twilio.rest import Client

from .config import settings

logger = logging.getLogger("restaurant_agent.twilio_client")

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def send_content_template(
    to_phone_e164: str, content_sid: str, content_variables: dict[str, str]
) -> dict:
    """Sends a pre-approved Content Template message (e.g. a special offer).

    to_phone_e164: bare E.164, e.g. "+971501234567" — "whatsapp:" is added
        here for the "to" address. settings.twilio_whatsapp_number already
        includes its own "whatsapp:" prefix, so it's used as-is for "from"
        (not double-prefixed).
    content_variables: maps Twilio's 1-indexed placeholder numbers, as
        strings, to their fill-in values — e.g.
        {"1": "Chicken Biriyani", "2": "12", "3": "6:00 PM", ...}.
    """
    client = _get_client()
    try:
        message = client.messages.create(
            from_=settings.twilio_whatsapp_number,
            to=f"whatsapp:{to_phone_e164}",
            content_sid=content_sid,
            content_variables=json.dumps(content_variables),
        )
        return {"status": "sent", "sid": message.sid, "to": to_phone_e164}
    except Exception as exc:
        logger.exception("Failed to send special offer message to %s", to_phone_e164)
        return {"status": "failed", "to": to_phone_e164, "error": str(exc)}
