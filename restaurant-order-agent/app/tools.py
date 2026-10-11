"""ADK tool functions for the restaurant order agent.

Each function here is registered as a Tool on the agent (see app/agent.py).
ADK inspects the function signature and docstring to decide when and how to
call it, so keep both accurate and specific — the docstring IS the tool
description the model sees, not just documentation for you.

Set DRY_RUN=true in .env (the default) to log calls instead of touching the
Sheet — useful for testing the agent's tool-picking behavior in isolation
before wiring up a real spreadsheet (see scripts/test_agent_locally.py).
"""
from __future__ import annotations

import logging
from typing import Any

from google.adk.tools import ToolContext

from . import sheets_client
from .config import settings

logger = logging.getLogger("restaurant_agent.tools")


def _caller_phone(tool_context: ToolContext) -> str:
    """The real, Twilio-verified E.164 number for the session this tool call belongs to.

    webhook.py sets the ADK session's user_id to the caller's number taken from
    Twilio's "From" field, so the model never has to be told or trusted with it —
    it can't invoke a tool without ADK first attaching that session's context.
    """
    return tool_context.session.user_id


def add_customer(name: str, tool_context: ToolContext, address: str = "") -> dict:
    """Add a new customer to the customer directory.

    Call this when a message comes from a phone number that isn't a known
    customer yet (check with get_customer first).

    Args:
        name: The customer's name, as given or inferred from the conversation.
        address: Delivery address, if the customer has already given one.

    Returns:
        A dict confirming the customer was added.
    """
    phone_e164 = _caller_phone(tool_context)
    logger.info("add_customer(name=%s, phone=%s)", name, phone_e164)
    if settings.dry_run:
        return {"status": "dry_run_ok", "name": name, "phone_e164": phone_e164}
    return sheets_client.add_customer(name=name, phone_e164=phone_e164, address=address)


def remove_customer(tool_context: ToolContext) -> dict:
    """Remove the current customer from future marketing broadcasts.

    Call this whenever a customer asks to stop receiving messages, unsubscribe,
    or be removed from the list. Treat any such request as final — don't ask
    the customer to confirm first, WhatsApp policy requires honoring it
    immediately.

    Returns:
        A dict with the result: "removed" or "not_found".
    """
    phone_e164 = _caller_phone(tool_context)
    logger.info("remove_customer(phone=%s)", phone_e164)
    if settings.dry_run:
        return {"status": "dry_run_ok", "phone_e164": phone_e164}
    return sheets_client.remove_customer(phone_e164=phone_e164)


def get_customer(tool_context: ToolContext) -> dict:
    """Look up the current customer by phone number.

    Use this early in a conversation to check whether the person messaging is
    already a known customer, and to reuse their saved delivery address
    instead of asking for it again.

    Returns:
        The customer's record if found, otherwise {"status": "not_found"}.
    """
    phone_e164 = _caller_phone(tool_context)
    logger.info("get_customer(phone=%s)", phone_e164)
    if settings.dry_run:
        return {"status": "dry_run_not_found"}
    record = sheets_client.find_customer_by_phone(phone_e164)
    return record or {"status": "not_found"}


def get_menu() -> dict:
    """Get today's available menu items, with prices and remaining quantity.

    ALWAYS call this before confirming any food order — never invent a dish
    or assume something is available. An item that has completely sold out
    for the day will simply not appear in this list (it is not shown as
    "sold out" separately — its absence IS the sold-out signal).

    Returns:
        A dict with "items": a list of
        {item_name, price, available_quantity, category} for every item
        that can currently be ordered.
    """
    logger.info("get_menu()")
    if settings.dry_run:
        return {
            "items": [
                {
                    "item_name": "Chicken Biryani",
                    "price": "250",
                    "available_quantity": 10,
                    "category": "Main",
                }
            ]
        }
    return {"items": sheets_client.get_menu()}


def record_order(
    customer_name: str,
    items: list[dict[str, Any]],
    delivery_address: str,
    tool_context: ToolContext,
    requested_time: str = "",
) -> dict:
    """Record a completed customer order and deduct it from menu stock.

    Only call this once you have confirmed, via get_menu(), that every item
    and quantity is actually available, and you have a delivery address.

    Do NOT use this for a customer confirming interest in the current
    special offer broadcast — that goes through record_offer_1_order
    instead, and is tracked separately.

    Args:
        customer_name: The customer's name.
        items: The order lines, each a dict with exactly two keys:
            "item_name" (str) — the EXACT name as it appears in get_menu(),
            never a paraphrase or guess, and "quantity" (int).
            Example: [{"item_name": "Chicken Biryani", "quantity": 2}].
        delivery_address: Where to deliver the order.
        requested_time: Requested delivery/pickup time, if the customer gave one.

    Returns:
        On success: {"status": "recorded", "order_id": ...} — confirm this
        back to the customer.
        On insufficient stock: {"status": "insufficient_stock", "details": [...]}
        — this can happen rarely if stock changed between your get_menu()
        check and this call. Each short item's entry includes "available":
        offer the customer that smaller quantity instead of failing outright.
    """
    phone_e164 = _caller_phone(tool_context)
    logger.info("record_order(phone=%s, items=%s)", phone_e164, items)

    if settings.dry_run:
        return {"status": "dry_run_ok", "order_id": "DRY-RUN-0001"}

    stock_result = sheets_client.decrement_menu_quantities(items)
    if not stock_result["ok"]:
        return {"status": "insufficient_stock", "details": stock_result["results"]}

    items_summary = ", ".join(
        f"{line['quantity']}x {line['item_name']}" for line in items
    )

    return sheets_client.record_order(
        customer_name=customer_name,
        phone_e164=phone_e164,
        items=items_summary,
        delivery_address=delivery_address,
        requested_time=requested_time,
    )


def get_active_offer_1() -> dict:
    """Check whether there's a currently active special offer broadcast.

    Call this whenever a message looks like a reply to a recent special-offer
    broadcast (e.g. "yes", "I'm interested", "count me in") rather than a
    normal food order, so you know what the customer is actually responding
    to before asking how many they want.

    Returns:
        A dict with "active": true and the offer's "item", "price",
        "pickup_from", "pickup_to", "pickup_date" if one is currently live,
        or {"active": false} if there isn't one right now — in that case,
        ask the customer a clarifying question instead of guessing what
        they mean.
    """
    logger.info("get_active_offer_1()")
    if settings.dry_run:
        return {
            "active": True,
            "item": "Chicken Biriyani",
            "price": "12",
            "pickup_from": "6:00 PM",
            "pickup_to": "8:00 PM",
            "pickup_date": "Saturday",
        }
    offer = sheets_client.get_active_offer_1()
    if offer is None:
        return {"active": False}
    return {"active": True, **offer}


def record_offer_1_order(quantity: int, tool_context: ToolContext) -> dict:
    """Record a customer's confirmed quantity for the current special offer.

    Only call this once the customer has confirmed they want the current
    special offer (via get_active_offer_1) AND told you how many they'd
    like — never guess or default the quantity. This writes to a separate
    tracking sheet from regular orders (offer_1_orders), so the restaurant
    can tell usual demand and special-offer demand apart; never call
    record_order for this instead.

    If the customer changes their mind later in the same offer round
    (e.g. "actually make it 5"), just call this again with the new
    quantity — it updates their existing confirmation rather than adding a
    duplicate.

    Args:
        quantity: How many the customer wants, as an explicit number they gave you.

    Returns:
        On success: {"status": "confirmed" or "updated", "offer": {...},
        "quantity": ...} — confirm the quantity and the offer's pickup
        window/date back to the customer in one short message.
        If the combined quantity for this offer has hit its limit:
        {"status": "offer_full", "offer": {...}, "remaining": N} — nothing
        was recorded. If "remaining" is greater than 0, apologize briefly
        and ask if that smaller amount works instead. If "remaining" is 0,
        let the customer know this offer is fully booked.
        If there's no active offer right now: {"status": "no_active_offer"}
        — tell the customer there's nothing currently on special offer.
    """
    phone_e164 = _caller_phone(tool_context)
    logger.info("record_offer_1_order(phone=%s, quantity=%s)", phone_e164, quantity)

    if settings.dry_run:
        return {
            "status": "dry_run_ok",
            "offer": {
                "item": "Chicken Biriyani",
                "price": "12",
                "pickup_from": "6:00 PM",
                "pickup_to": "8:00 PM",
                "pickup_date": "Saturday",
            },
            "quantity": quantity,
        }

    customer = sheets_client.find_customer_by_phone(phone_e164) or {}
    customer_name = customer.get("name", "")

    return sheets_client.record_offer_1_order(
        phone_e164=phone_e164, customer_name=customer_name, quantity=quantity
    )
