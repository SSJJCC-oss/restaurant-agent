"""Thin wrapper around the Google Sheets API for the Customers/Orders/Menu sheets.

Expected sheet layout (matches the project design doc, order-module-design.md):

  Customers!A:J -> customer_id | name | phone_e164 | default_address | opted_in
                   | date_added | date_removed | source | notes | location

  Orders!A:K     -> order_id | timestamp | customer_id | customer_name | phone
                   | items | delivery_address | requested_time | price_estimate
                   | status | notes

  Menu!A:F       -> item_name | price | available_quantity | category
                   | is_active | notes

  offer_1!A:I    -> item | price | pickup_from | pickup_to | pickup_date
                   | status | first_seen_at | offer_id | max_quantity

  offer_1_orders!A:J -> order_id | timestamp | customer_id | customer_name
                   | phone_e164 | offer_item | quantity | pickup_date | status
                   | offer_id

Auth: local dev uses GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account
JSON key that has been shared (as an Editor) on the target spreadsheet. In Cloud
Run, skip the key file entirely and attach the service account to the Cloud Run
service instead — Application Default Credentials picks it up automatically.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import google.auth
import google.auth.exceptions
from googleapiclient.discovery import build

from .config import settings

logger = logging.getLogger("restaurant_agent.sheets_client")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CUSTOMERS_RANGE = "Customers!A:J"
ORDERS_RANGE = "Orders!A:K"
MENU_RANGE = "Menu!A:F"
OFFER_1_RANGE = "offer_1!A:I"
OFFER_1_ORDERS_RANGE = "offer_1_orders!A:J"

_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service
    try:
        creds, _ = google.auth.default(scopes=SCOPES)
    except google.auth.exceptions.DefaultCredentialsError as exc:
        raise RuntimeError(
            "No Google credentials found. For local dev, set "
            "GOOGLE_APPLICATION_CREDENTIALS in .env to a service-account key file "
            "that has been shared on the target Sheet (see README.md step 2)."
        ) from exc
    _service = build("sheets", "v4", credentials=creds)
    return _service


def _read(range_name: str) -> list[list[str]]:
    service = _get_service()
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=settings.sheets_spreadsheet_id, range=range_name)
        .execute()
    )
    return result.get("values", [])


def _append(range_name: str, row: list) -> None:
    service = _get_service()
    service.spreadsheets().values().append(
        spreadsheetId=settings.sheets_spreadsheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _text_literal(value: str) -> str:
    """Force Sheets to store `value` as literal text, not a formula/number.

    With valueInputOption="USER_ENTERED" (used by _append), Sheets parses
    each value the way a human typing it would — a leading "+" (or "-" or
    "=") starts a formula, so an E.164 phone number like "+971555093240"
    gets silently evaluated as unary-plus and stored as the bare number
    971555093240, stripping the "+". That broke phone-number lookups
    (find_customer_by_phone never matched) — see
    claude/cloud-shell-setup-progress.md, 2026-09-19.

    A leading apostrophe is Sheets' own "treat the rest as literal text"
    signal; it is never stored or returned by the API, so callers get the
    exact string back on every subsequent read.
    """
    if value.startswith(("+", "-", "=")):
        return f"'{value}"
    return value


def _next_customer_id() -> str:
    """Returns the next sequential customer_id, e.g. "001", "042", "103".

    Scans existing Customers rows for customer_id values that are purely
    digits — this deliberately skips old rows where customer_id was set to
    the phone number (e.g. "+971555093240"), since those don't match a
    digits-only pattern. Takes the highest such number and adds 1; starts
    at "001" if none exist yet. Zero-padded to 3 digits; naturally grows
    past "999" if the restaurant ever gets that many customers (e.g.
    "1000") rather than erroring or wrapping — revisit padding then.
    """
    rows = _read(CUSTOMERS_RANGE)
    if not rows:
        return "001"
    header, *data = rows
    max_id = 0
    for row in data:
        record = dict(zip(header, row))
        cid = (record.get("customer_id") or "").strip()
        if cid.isdigit():
            max_id = max(max_id, int(cid))
    return f"{max_id + 1:03d}"


def _find_customer_row_index(phone_e164: str) -> Optional[int]:
    """Returns the 1-indexed sheet row number (including the header row), or None."""
    rows = _read(CUSTOMERS_RANGE)
    if not rows:
        return None
    header, *data = rows
    for i, row in enumerate(data, start=2):  # data starts at sheet row 2
        record = dict(zip(header, row))
        if record.get("phone_e164") == phone_e164:
            return i
    return None


def find_customer_by_phone(phone_e164: str) -> Optional[dict]:
    rows = _read(CUSTOMERS_RANGE)
    if not rows:
        return None
    header, *data = rows
    for row in data:
        record = dict(zip(header, row))
        if record.get("phone_e164") == phone_e164:
            return record
    return None


def add_customer(name: str, phone_e164: str, address: str = "") -> dict:
    customer_id = _next_customer_id()
    row = [
        customer_id,
        name,
        _text_literal(phone_e164),
        address,
        "Y",
        dt.date.today().isoformat(),
        "",
        "self-registered via WhatsApp",
        "",
    ]
    _append(CUSTOMERS_RANGE, row)
    return {"status": "added", "phone_e164": phone_e164, "customer_id": customer_id}


def remove_customer(phone_e164: str) -> dict:
    row_idx = _find_customer_row_index(phone_e164)
    if row_idx is None:
        return {"status": "not_found", "phone_e164": phone_e164}

    service = _get_service()
    today = dt.date.today().isoformat()
    # opted_in (col E) and date_removed (col G) are not adjacent, so two updates.
    service.spreadsheets().values().update(
        spreadsheetId=settings.sheets_spreadsheet_id,
        range=f"Customers!E{row_idx}",
        valueInputOption="USER_ENTERED",
        body={"values": [["N"]]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=settings.sheets_spreadsheet_id,
        range=f"Customers!G{row_idx}",
        valueInputOption="USER_ENTERED",
        body={"values": [[today]]},
    ).execute()
    return {"status": "removed", "phone_e164": phone_e164}


def set_customer_location(phone_e164: str, latitude: float, longitude: float) -> dict:
    """Records a customer's GPS location as a tappable Google Maps link.

    Called directly from the webhook when an inbound WhatsApp message is a
    native location share (Latitude/Longitude present on the Twilio
    request) — bypasses the ADK agent entirely, since this is a structured
    message type, not something for the LLM to interpret from text. See
    claude/cloud-shell-setup-progress.md, "Customer location" design.

    Overwrites any previously recorded location for this customer, so
    re-sharing location (e.g. after moving) updates it in place.
    """
    row_idx = _find_customer_row_index(phone_e164)
    if row_idx is None:
        return {"status": "not_found", "phone_e164": phone_e164}

    maps_url = f"https://www.google.com/maps?q={latitude},{longitude}"
    service = _get_service()
    service.spreadsheets().values().update(
        spreadsheetId=settings.sheets_spreadsheet_id,
        range=f"Customers!J{row_idx}",  # location
        valueInputOption="USER_ENTERED",
        body={"values": [[maps_url]]},
    ).execute()
    return {"status": "updated", "phone_e164": phone_e164, "location_url": maps_url}


def get_menu() -> list[dict]:
    """Returns every menu item that is active and currently in stock.

    An item with available_quantity of 0, or is_active set to "N", is left
    out entirely — by design (see claude/cloud-shell-setup-progress.md,
    Pending Feature To-Do List item 4, decision 3).
    """
    rows = _read(MENU_RANGE)
    if not rows:
        return []
    header, *data = rows
    items = []
    for row in data:
        record = dict(zip(header, row))
        is_active = record.get("is_active", "Y").strip().upper() != "N"
        try:
            qty = int(record.get("available_quantity", 0) or 0)
        except ValueError:
            qty = 0
        if is_active and qty > 0:
            items.append(
                {
                    "item_name": record.get("item_name", ""),
                    "price": record.get("price", ""),
                    "available_quantity": qty,
                    "category": record.get("category", ""),
                }
            )
    return items


def decrement_menu_quantities(items: list[dict]) -> dict:
    """Attempts to decrement available_quantity for each requested order line.

    items: a list of {"item_name": str, "quantity": int}.

    All-or-nothing: if any requested line can't be fully satisfied (item not
    found, or not enough stock), NOTHING is decremented for any line, and
    the returned "results" tell the caller exactly what's short so it can
    offer a reduced quantity instead. This avoids partially fulfilling one
    order line while rejecting another from the same message.
    """
    rows = _read(MENU_RANGE)
    if not rows:
        return {
            "ok": False,
            "results": [
                {"item_name": line["item_name"], "status": "not_found", "available": 0}
                for line in items
            ],
        }
    header, *data = rows

    results = []
    all_ok = True
    to_update: list[tuple[int, int]] = []  # (sheet row index, new quantity)

    for line in items:
        name = str(line["item_name"]).strip()
        requested = int(line["quantity"])
        row_idx = None
        available = 0
        for i, row in enumerate(data, start=2):
            record = dict(zip(header, row))
            if record.get("item_name", "").strip().lower() == name.lower():
                row_idx = i
                try:
                    available = int(record.get("available_quantity", 0) or 0)
                except ValueError:
                    available = 0
                break

        if row_idx is None:
            all_ok = False
            results.append({"item_name": name, "status": "not_found", "available": 0})
        elif available < requested:
            all_ok = False
            results.append(
                {
                    "item_name": name,
                    "status": "insufficient",
                    "requested": requested,
                    "available": available,
                }
            )
        else:
            to_update.append((row_idx, available - requested))
            results.append({"item_name": name, "status": "ok", "requested": requested})

    if not all_ok:
        return {"ok": False, "results": results}

    service = _get_service()
    for row_idx, new_qty in to_update:
        service.spreadsheets().values().update(
            spreadsheetId=settings.sheets_spreadsheet_id,
            range=f"Menu!C{row_idx}",  # available_quantity column
            valueInputOption="USER_ENTERED",
            body={"values": [[new_qty]]},
        ).execute()

    return {"ok": True, "results": results}


def record_order(
    customer_name: str,
    phone_e164: str,
    items: str,
    delivery_address: str,
    requested_time: str = "",
) -> dict:
    customer = find_customer_by_phone(phone_e164)
    customer_id = customer.get("customer_id") if customer else phone_e164
    order_id = f"{phone_e164}-{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    row = [
        _text_literal(order_id),
        dt.datetime.utcnow().isoformat(),
        _text_literal(customer_id),
        customer_name,
        _text_literal(phone_e164),
        items,
        delivery_address,
        requested_time,
        "",  # price_estimate — left blank in v1
        "received",
        "",
    ]
    _append(ORDERS_RANGE, row)
    return {"status": "recorded", "order_id": order_id}


def _offer_1_row_records() -> list[tuple[int, dict]]:
    """Returns (sheet_row_index, record) for every offer_1 row, 1-indexed including header."""
    rows = _read(OFFER_1_RANGE)
    if not rows:
        return []
    header, *data = rows
    return [(i, dict(zip(header, row))) for i, row in enumerate(data, start=2)]


def stamp_offer_1_first_seen(row_idx: int, timestamp_iso: str) -> None:
    """Writes timestamp_iso to offer_1!G, forced to literal text.

    Without the leading-apostrophe force, Sheets auto-parses an ISO string
    written with USER_ENTERED into its own date/time value and redisplays
    it using whatever format it lands on for that cell — which was
    observed to differ row-to-row (e.g. "2026-10-03 18:05:01" on one row,
    "03/10/2026 18:21:01" on another, seemingly locale/column-formatting
    dependent). get_due_offer_1() parses this value with
    datetime.fromisoformat(), which can't read the D/M/Y form at all — a
    row that landed on that format would silently fail to parse and never
    be picked up as due again, with no visible error. Forcing literal
    text (same trick as _text_literal() for phone numbers) guarantees the
    exact string written is the exact string read back, regardless of
    Sheets' own formatting/locale behavior. See
    claude/cloud-shell-setup-progress.md, 2026-10-03.
    """
    service = _get_service()
    service.spreadsheets().values().update(
        spreadsheetId=settings.sheets_spreadsheet_id,
        range=f"offer_1!G{row_idx}",  # first_seen_at
        valueInputOption="USER_ENTERED",
        body={"values": [[f"'{timestamp_iso}"]]},
    ).execute()


def mark_offer_1_sent(row_idx: int) -> None:
    service = _get_service()
    service.spreadsheets().values().update(
        spreadsheetId=settings.sheets_spreadsheet_id,
        range=f"offer_1!F{row_idx}",  # status
        valueInputOption="USER_ENTERED",
        body={"values": [["sent"]]},
    ).execute()


def get_due_offer_1(min_age_seconds: int = 3600) -> list[dict]:
    """Special offers ready to send: first_seen_at set, older than
    min_age_seconds, not yet sent.

    Also stamps first_seen_at on any row that doesn't have one yet, then
    leaves it alone — a plain Sheets edit fires no webhook, so this is what
    notices a new row in the first place, and that stamp starts the
    correction buffer the user asked for (see
    claude/cloud-shell-setup-progress.md, "Campaign engine", 2026-09-20).
    """
    due = []
    for row_idx, record in _offer_1_row_records():
        item = (record.get("item") or "").strip()
        if not item:
            continue  # blank row

        status = (record.get("status") or "").strip().lower()
        if status == "sent":
            continue

        first_seen = (record.get("first_seen_at") or "").strip()
        if not first_seen:
            now_iso = dt.datetime.utcnow().isoformat()
            stamp_offer_1_first_seen(row_idx, now_iso)
            continue  # don't send in the same pass it was first seen

        try:
            seen_at = dt.datetime.fromisoformat(first_seen)
        except ValueError:
            logger.warning(
                "offer_1 row %d: could not parse first_seen_at %r as a "
                "timestamp — this row will never be sent until its "
                "first_seen_at cell is fixed or cleared.",
                row_idx,
                first_seen,
            )
            continue

        age = (dt.datetime.utcnow() - seen_at).total_seconds()
        if age >= min_age_seconds:
            due.append(
                {
                    "row_idx": row_idx,
                    "item": item,
                    "price": record.get("price", ""),
                    "pickup_from": record.get("pickup_from", ""),
                    "pickup_to": record.get("pickup_to", ""),
                    "pickup_date": record.get("pickup_date", ""),
                }
            )
    return due


def get_active_offer_1() -> Optional[dict]:
    """Returns the most recently sent special offer that's still "live",
    or None if there isn't one.

    "Live" means status == "sent" (not "closed", and not still blank/draft
    or already superseded). Staff manually edit a row's status to "closed"
    once that evening's pickup window has passed — see
    claude/cloud-shell-setup-progress.md, "offer_1_orders" design. Only one
    offer is ever considered active at a time: if more than one row is
    "sent" and not "closed" (shouldn't normally happen), the one with the
    most recent first_seen_at wins.
    """
    candidates = []
    for _row_idx, record in _offer_1_row_records():
        item = (record.get("item") or "").strip()
        if not item:
            continue
        status = (record.get("status") or "").strip().lower()
        if status != "sent":
            continue
        first_seen = (record.get("first_seen_at") or "").strip()
        candidates.append((first_seen, record))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0])
    _first_seen, record = candidates[-1]
    return {
        "item": record.get("item", ""),
        "price": record.get("price", ""),
        "pickup_from": record.get("pickup_from", ""),
        "pickup_to": record.get("pickup_to", ""),
        "pickup_date": record.get("pickup_date", ""),
        "offer_id": record.get("offer_id", ""),
        "max_quantity": record.get("max_quantity", ""),
    }


def _offer_1_total_confirmed_quantity(offer_id: str, exclude_phone: str = "") -> int:
    """Sums the quantity already confirmed by every OTHER customer for this
    offer_id — exclude_phone's own existing row (if any) is left out, since
    record_offer_1_order() needs "everyone else's total" to work out how
    much headroom is left for the customer currently confirming, including
    when they're updating their own earlier confirmation.
    """
    rows = _read(OFFER_1_ORDERS_RANGE)
    if not rows:
        return 0
    header, *data = rows
    total = 0
    for row in data:
        record = dict(zip(header, row))
        if record.get("offer_id") != offer_id:
            continue
        if exclude_phone and record.get("phone_e164") == exclude_phone:
            continue
        try:
            total += int(record.get("quantity", 0) or 0)
        except ValueError:
            pass
    return total


def _find_offer_1_order_row_index(phone_e164: str, offer_id: str) -> Optional[int]:
    """Returns the 1-indexed sheet row for this customer's existing
    confirmation against this specific offer round (matched by phone +
    offer_id, not pickup_date — two different offer rounds can share the
    same pickup date, so offer_id is the only reliable key), or None if
    they haven't confirmed this one yet. Pre-existing rows written before
    offer_id existed have a blank offer_id and simply won't match, which
    is an acceptable one-time gap rather than a bug to work around.
    """
    rows = _read(OFFER_1_ORDERS_RANGE)
    if not rows:
        return None
    header, *data = rows
    for i, row in enumerate(data, start=2):
        record = dict(zip(header, row))
        if (
            record.get("phone_e164") == phone_e164
            and record.get("offer_id") == offer_id
        ):
            return i
    return None


def record_offer_1_order(phone_e164: str, customer_name: str, quantity: int) -> dict:
    """Records (or updates) a customer's confirmed quantity against the
    currently active special offer, enforcing the offer's combined
    (restaurant-wide) max_quantity cap across all customers if one is set.

    Upserts on (phone_e164, offer_id): if this customer already has a row
    for the currently active offer round, its quantity (and timestamp) is
    updated in place rather than adding a duplicate row — so changing
    their mind ("actually make it 5") corrects the existing confirmation
    instead of double-counting demand.
    """
    offer = get_active_offer_1()
    if offer is None:
        logger.info("record_offer_1_order(phone=%s): no active offer", phone_e164)
        return {"status": "no_active_offer"}

    offer_id = offer.get("offer_id", "")
    pickup_date = offer["pickup_date"]

    max_quantity_raw = (offer.get("max_quantity") or "").strip()
    if max_quantity_raw.isdigit():
        max_quantity = int(max_quantity_raw)
        already_confirmed = _offer_1_total_confirmed_quantity(
            offer_id, exclude_phone=phone_e164
        )
        remaining = max_quantity - already_confirmed
        if quantity > remaining:
            logger.info(
                "record_offer_1_order(phone=%s, offer_id=%s): requested %d "
                "exceeds remaining %d (max %d, already confirmed by others %d)",
                phone_e164, offer_id, quantity, remaining, max_quantity, already_confirmed,
            )
            return {"status": "offer_full", "offer": offer, "remaining": max(remaining, 0)}

    now_iso = dt.datetime.utcnow().isoformat()
    existing_row_idx = _find_offer_1_order_row_index(phone_e164, offer_id)

    try:
        if existing_row_idx is not None:
            service = _get_service()
            # timestamp (col B) and quantity (col G) are not adjacent, so two updates.
            service.spreadsheets().values().update(
                spreadsheetId=settings.sheets_spreadsheet_id,
                range=f"offer_1_orders!B{existing_row_idx}",
                valueInputOption="USER_ENTERED",
                body={"values": [[now_iso]]},
            ).execute()
            service.spreadsheets().values().update(
                spreadsheetId=settings.sheets_spreadsheet_id,
                range=f"offer_1_orders!G{existing_row_idx}",
                valueInputOption="USER_ENTERED",
                body={"values": [[quantity]]},
            ).execute()
            logger.info(
                "record_offer_1_order(phone=%s, offer_id=%s): updated existing "
                "offer_1_orders row %d to quantity=%d",
                phone_e164, offer_id, existing_row_idx, quantity,
            )
            return {"status": "updated", "offer": offer, "quantity": quantity}

        customer = find_customer_by_phone(phone_e164)
        customer_id = customer.get("customer_id") if customer else phone_e164
        order_id = f"{phone_e164}-offer1-{offer_id or pickup_date}"
        row = [
            _text_literal(order_id),
            now_iso,
            _text_literal(customer_id),
            customer_name,
            _text_literal(phone_e164),
            offer["item"],
            quantity,
            pickup_date,
            "confirmed",
            offer_id,
        ]
        logger.info(
            "record_offer_1_order(phone=%s, offer_id=%s): appending new "
            "offer_1_orders row %r",
            phone_e164, offer_id, row,
        )
        _append(OFFER_1_ORDERS_RANGE, row)
        logger.info(
            "record_offer_1_order(phone=%s, offer_id=%s): append call to %s completed",
            phone_e164, offer_id, OFFER_1_ORDERS_RANGE,
        )
        return {"status": "confirmed", "offer": offer, "quantity": quantity}
    except Exception:
        logger.exception(
            "record_offer_1_order(phone=%s, offer_id=%s): write to offer_1_orders failed",
            phone_e164, offer_id,
        )
        raise


def get_opted_in_customers() -> list[dict]:
    rows = _read(CUSTOMERS_RANGE)
    if not rows:
        return []
    header, *data = rows
    customers = []
    for row in data:
        record = dict(zip(header, row))
        if (record.get("opted_in") or "").strip().upper() == "Y":
            customers.append(record)
    return customers