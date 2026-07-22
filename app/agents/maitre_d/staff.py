"""Staff-facing reservations operations: the door (seat/complete/no-show),
waitlist/reservation listings, VIP management, and free-form natural-
language Q&A about bookings. Separate from agent.py's MaitreD engine, which
is the guest-facing booking conversation only -- mirrors how scout/
reputation/revenue split their staff-facing surface from the customer-
facing one.
"""

from __future__ import annotations

import logging

from app.agents.maitre_d.agent import get_maitre_d
from app.agents.maitre_d.config import VenueConfig, normalise_phone
from app.agents.maitre_d.store import Store

logger = logging.getLogger(__name__)


# ── door commands ────────────────────────────────────────────────────────────

def _resolve_reservation_ref(store: Store, ref: str) -> str | None:
    """A staff member typing the full "res_xxxxxxxxxxxx" id is impractical
    over WhatsApp -- accept any suffix that uniquely identifies one active
    reservation instead (e.g. "seat a1b2c3" or even "seat b3" if that's
    enough to be unique). Returns the full reservation_id, or None if no
    active reservation matches (ambiguous or not found)."""
    ref = ref.strip().lower().removeprefix("res_")
    if not ref:
        return None
    candidates = [
        r for r in store.list_reservations()
        if r.status in ("pending", "confirmed", "seated")
        and r.reservation_id.lower().endswith(ref)
    ]
    if len(candidates) == 1:
        return candidates[0].reservation_id
    return None


def handle_door_command(store_id: int, first_word: str, rest: str) -> str | None:
    """Try to handle this as a seat/complete/no-show door command. Returns
    None if `first_word` doesn't match one, so the caller can fall through
    to something else."""
    action_map = {"seat": "seated", "complete": "completed", "noshow": "no_show"}
    if first_word == "no" and rest.lower().startswith("show"):
        first_word, rest = "noshow", rest[4:].strip()
    if first_word not in action_map:
        return None

    store = Store(store_id)
    ref = rest.strip()
    if not ref:
        return "Which reservation? Reply e.g. \"seat a1b2c3\" using the last few characters of the booking id from *reservations*."
    reservation_id = _resolve_reservation_ref(store, ref)
    if reservation_id is None:
        return f"Couldn't find a unique active reservation matching \"{ref}\". Check *reservations* for the exact id."

    md = get_maitre_d(store_id)
    action = action_map[first_word]
    res = {
        "seated": md.mark_seated, "completed": md.mark_completed, "no_show": md.mark_no_show,
    }[action](reservation_id)
    if res is None:
        return "That reservation isn't in a state that allows that action right now."
    label = {"seated": "Seated", "completed": "Marked completed", "no_show": "Marked no-show"}[action]
    return f"{label}: {res.name or res.phone}, party {res.party_size}, table {res.table_id or 'n/a'}."


# ── listings ──────────────────────────────────────────────────────────────────

def format_reservations(store_id: int, upcoming_only: bool = True) -> str:
    from datetime import datetime, timedelta
    store = Store(store_id)
    rows = [
        r for r in store.list_reservations()
        if r.status in ("pending", "confirmed", "seated")
    ]
    if upcoming_only:
        rows = [r for r in rows if r.when >= datetime.now() - timedelta(hours=6)]
    rows.sort(key=lambda r: r.when)
    if not rows:
        return "No upcoming reservations on the book."
    lines = ["Upcoming reservations:"]
    for r in rows:
        tag = r.reservation_id.replace("res_", "")[-6:]
        vip = " (VIP)" if r.is_vip else ""
        dep = " [deposit pending]" if r.status == "pending" else ""
        branch = f" — {r.branch_name}" if r.branch_name else ""
        lines.append(
            f"• [{tag}] {r.name or r.phone}, party {r.party_size}, "
            f"{r.when.strftime('%a %d %b %I:%M %p').replace(' 0', ' ')}, "
            f"table {r.table_id or 'n/a'}{branch}{vip}{dep}"
        )
    lines.append("\nSay \"seat/complete/noshow <id>\" using the bracketed tag to update one.")
    return "\n".join(lines)


def format_waitlist(store_id: int) -> str:
    store = Store(store_id)
    rows = store.list_waitlist(status="waiting")
    if not rows:
        return "The waitlist is empty."
    lines = ["Waitlist:"]
    for w in rows:
        vip = " (VIP)" if w.is_vip else ""
        branch = f" — {w.branch_name}" if w.branch_name else ""
        lines.append(
            f"• {w.name or w.phone}, party {w.party_size}, wants "
            f"{w.requested_when.strftime('%a %d %b %I:%M %p').replace(' 0', ' ')}{branch}{vip}"
        )
    return "\n".join(lines)


def format_vips(store_id: int) -> str:
    cfg = VenueConfig.load(store_id)
    if not cfg.vips:
        return "No VIPs on file yet. Add one: \"add vip +923001234567 Ayesha Khan, food critic\"."
    lines = ["VIP list:"]
    for phone, v in cfg.vips.items():
        notes = f" — {v.notes}" if v.notes else ""
        lines.append(f"• {v.name or phone} ({v.tier}), {phone}{notes}")
    return "\n".join(lines)


def format_locations(store_id: int) -> str:
    locations = VenueConfig.list_locations(store_id)
    if not locations:
        return "This restaurant has a single location, no separate branches configured."
    lines = ["Branches:"]
    for loc in locations:
        tag = "primary" if loc.is_primary else "branch"
        booking = "takes reservations" if loc.accepts_reservations else "delivery-only, no reservations"
        addr = f" — {loc.address}" if loc.address else ""
        lines.append(f"• {loc.branch_name} ({tag}, {booking}){addr}")
    return "\n".join(lines)


def add_vip(store_id: int, rest: str) -> str:
    """Parse "add vip <phone> <name>[, notes]" and save it."""
    from app.core.db import SessionLocal, MaitreDVip

    parts = rest.strip().split(None, 1)
    if not parts:
        return "Usage: add vip <phone> <name>[, notes]"
    phone = normalise_phone(parts[0])
    if not phone.startswith("+") or len(phone) < 8:
        return "That doesn't look like a phone number. Usage: add vip +923001234567 Ayesha Khan, food critic"
    rest2 = parts[1] if len(parts) > 1 else ""
    if "," in rest2:
        name, notes = rest2.split(",", 1)
    else:
        name, notes = rest2, ""
    name, notes = name.strip(), notes.strip()
    if not name:
        return "Usage: add vip <phone> <name>[, notes]"

    with SessionLocal() as db:
        row = db.query(MaitreDVip).filter(
            MaitreDVip.store_id == store_id, MaitreDVip.phone == phone,
        ).first()
        if row:
            row.name, row.notes = name, notes
        else:
            db.add(MaitreDVip(store_id=store_id, phone=phone, name=name, tier="vip", notes=notes))
        db.commit()
    return f"Added {name} ({phone}) to the VIP list."


# ── natural-language Q&A ────────────────────────────────────────────────────

def answer_question(store_id: int, text: str, history: list[dict] | None = None) -> str:
    """Free-form Q&A grounded in the real reservation/waitlist book, same
    single-LLM-call pattern as integrity/revenue/reputation's
    answer_question()."""
    try:
        from app.core.llm import get_client, get_model, nothink_kwargs
        from app.core.persona import staff_persona
        client = get_client()
    except Exception:
        return "Reservations Q&A is unavailable right now, but you can still ask for *reservations*, *waitlist*, or *vip list*."

    context = "\n\n".join([
        format_locations(store_id), format_reservations(store_id),
        format_waitlist(store_id), format_vips(store_id),
    ])
    system = (
        staff_persona("You answer staff questions about reservations, the waitlist and VIP guests.", store_id)
        + "\nCite exact names, times and table numbers from the data; do not invent bookings."
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": f"{context}\n\nQuestion: {text}"})
    try:
        model = get_model()
        resp = client.chat.completions.create(
            timeout=25.0, model=model, max_tokens=400,
            messages=messages, **nothink_kwargs(model),
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("maitre_d.answer_question: store=%d failed: %s", store_id, exc)
        return "Couldn't pull that up right now (a temporary hiccup on our end). Please try again in a moment."
