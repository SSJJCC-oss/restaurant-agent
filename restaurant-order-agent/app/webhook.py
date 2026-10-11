"""FastAPI app exposing the Twilio WhatsApp webhook.

Run locally:
    uvicorn app.webhook:app --reload --port 8080

Then expose it publicly for Twilio to reach (e.g. `ngrok http 8080`) and set
that URL + "/twilio/whatsapp" as the sandbox's "WHEN A MESSAGE COMES IN"
webhook in the Twilio console.
"""
import logging

from fastapi import FastAPI, Request, Response
from google.adk.runners import Runner
from google.genai import types
from twilio.twiml.messaging_response import MessagingResponse

from app.agent import root_agent
from app.config import settings
from app.firestore_session_service import FirestoreSessionService
from app import sheets_client, twilio_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("restaurant_agent.webhook")

APP_NAME = "restaurant_order_agent"

# Twilio Content SID for the "daily_offer" special-offer template — plain
# text, no buttons, approved for WhatsApp business-initiated use 2026-09-28.
# See claude/cloud-shell-setup-progress.md, "Campaign engine".
OFFER_1_CONTENT_SID = "HX112bfe12f1aa9d6ccafadb81386a3ce5"

app = FastAPI()

# Persistent, Firestore-backed session storage — survives Cloud Run
# restarts/cold starts and is shared correctly across concurrent instances.
# See claude/cloud-shell-setup-progress.md, "Persistent session storage".
session_service = FirestoreSessionService(project="restaurant-order-agent-prod")
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)


@app.post("/twilio/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    from_number = form.get("From", "")  # e.g. "whatsapp:+971501234567"
    body = form.get("Body", "")
    latitude = form.get("Latitude")
    longitude = form.get("Longitude")

    phone = from_number.removeprefix("whatsapp:").strip()

    logger.info("Inbound WhatsApp message from %s: %s", phone, body)

    # A native WhatsApp location share arrives as Latitude/Longitude, not
    # text — handle it directly rather than routing it through the agent,
    # which has no way to "read" an attachment. See
    # claude/cloud-shell-setup-progress.md, "Customer location" design.
    if latitude and longitude:
        try:
            location_result = sheets_client.set_customer_location(
                phone, float(latitude), float(longitude)
            )
        except Exception:
            logger.exception("Failed to record location for %s", phone)
            location_result = {"status": "error"}

        if location_result.get("status") == "updated":
            reply_text = "📍 Got it — thanks! We'll use this to find you for delivery."
        elif location_result.get("status") == "not_found":
            reply_text = (
                "Thanks for the location! Could you first say hello so I can "
                "get you registered?"
            )
        else:
            reply_text = (
                "Sorry, something went wrong saving your location — please "
                "try again in a moment."
            )

        twiml = MessagingResponse()
        twiml.message(reply_text)
        return Response(content=str(twiml), media_type="application/xml")

    try:
        session = await session_service.get_session(app_name=APP_NAME, user_id=phone, session_id=phone)
    except Exception:
        session = None

    if session is None:
        try:
            await session_service.create_session(app_name=APP_NAME, user_id=phone, session_id=phone)
        except Exception:
            logger.exception("Failed to create session for %s", phone)


    reply_text = "Sorry, something went wrong on our end — please try again in a moment."
    content = types.Content(role="user", parts=[types.Part(text=body)])
    try:
        async for event in runner.run_async(user_id=phone, session_id=phone, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                reply_text = event.content.parts[0].text
    except Exception:
        # Any failure here (a tool error, a Sheets permission issue, etc.)
        # would otherwise crash the request before a reply is ever sent —
        # Twilio gets nothing and the customer sees total silence. Log it
        # and fall back to the default reply_text instead.
        logger.exception("Agent run failed for %s", phone)

    twiml = MessagingResponse()
    twiml.message(reply_text)
    return Response(content=str(twiml), media_type="application/xml")


@app.post("/offer_1/check-and-send")
async def check_and_send_offer_1():
    """Polled by Cloud Scheduler every ~1 minute.

    A brand-new offer_1 row gets its first_seen_at stamped and is left
    alone — that's the 1-minute buffer for corrections/second thoughts
    (shortened 2026-10-03 from an original 5 minutes). A row whose
    first_seen_at is >=1 minute old and not yet "sent" goes out as a
    WhatsApp marketing broadcast (the daily_offer Content Template) to
    every opted-in customer, then gets marked "sent" so it's never resent.

    NOTE: unauthenticated for now — security hardening (Secret Manager,
    endpoint auth) was explicitly deferred by the user on 2026-09-20 until
    the rest of the system is streamlined. Anyone who discovers this URL
    could trigger an early send; revisit once other things are stable.
    See claude/cloud-shell-setup-progress.md, "Campaign engine".
    """
    due = sheets_client.get_due_offer_1(min_age_seconds=60)
    if not due:
        return {"status": "ok", "sent_offers": 0}

    customers = sheets_client.get_opted_in_customers()
    results = []

    for offer in due:
        content_variables = {
            "1": offer["item"],
            "2": str(offer["price"]),
            "3": offer["pickup_from"],
            "4": offer["pickup_to"],
            "5": offer["pickup_date"],
        }

        sent_count = 0
        failed = []
        for customer in customers:
            phone = customer.get("phone_e164", "")
            if not phone:
                continue
            if settings.dry_run:
                logger.info("[DRY RUN] Would send special offer %r to %s", offer["item"], phone)
                sent_count += 1
                continue
            result = twilio_client.send_content_template(phone, OFFER_1_CONTENT_SID, content_variables)
            if result["status"] == "sent":
                sent_count += 1
            else:
                failed.append(result)

        sheets_client.mark_offer_1_sent(offer["row_idx"])
        results.append(
            {
                "item": offer["item"],
                "sent_count": sent_count,
                "failed_count": len(failed),
            }
        )
        logger.info(
            "Special offer %r sent to %d/%d customers (%d failed)",
            offer["item"],
            sent_count,
            len(customers),
            len(failed),
        )

    return {"status": "ok", "sent_offers": len(results), "results": results}


@app.get("/health-check")
async def healthz():
    return {"status": "ok"}
