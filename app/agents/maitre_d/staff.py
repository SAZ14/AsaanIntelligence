"""Staff-facing queue operations: view the live line, admit the next
guest, remove/insert a position, VIP management, and free-form natural-
language Q&A about the queue. Separate from agent.py's MaitreD engine,
which is the guest-facing "join the queue" conversation only -- mirrors
how scout/reputation/revenue split their staff-facing surface from the
customer-facing one.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from app.agents.maitre_d.config import VenueConfig, normalise_phone, match_location, _join_or
from app.agents.maitre_d.models import QueueEntry
from app.agents.maitre_d.store import Store

logger = logging.getLogger(__name__)


# ── branch resolution for staff queue commands ──────────────────────────────
# "admit"/"remove <position>"/"add <position> ..." all act on ONE location's
# ordering, so a multi-branch store must say which one -- unlike the guest
# booking flow (agent.py), which can infer a branch from anywhere in a free-
# form sentence, staff shorthand uses a plain "... at <branch>" suffix so
# parsing the position/phone/name in front of it stays unambiguous.

def _split_branch_suffix(text: str) -> tuple[str, str]:
    m = re.search(r"\bat\b", text, flags=re.IGNORECASE)
    if not m:
        return text.strip(), ""
    return text[:m.start()].strip(), text[m.end():].strip()


def _venue_now(store_id: int, location_id: int | None) -> datetime:
    """The specific location's own venue-local clock (falls back to the
    store's primary/default location if `location_id` doesn't resolve) --
    staff actions have no MaitreD instance's self._now() to reuse, but
    still need the SAME clock the guest-side guards compare against
    (see Store.admit_next's docstring on why raw UTC would skew things)."""
    if location_id:
        loc = next(
            (l for l in VenueConfig.list_locations(store_id) if l.location_id == location_id), None,
        )
        if loc is not None:
            return loc.now()
    return VenueConfig.load(store_id).now()


def _resolve_queue_location(store_id: int, text: str) -> tuple[int | None, str, str | None]:
    """Returns (location_id_or_None, remaining_text_with_branch_stripped,
    error_reply_or_None). Single-location stores never need a branch named
    -- location_id stays None, matching how a lone-location guest's
    MaitreDQueueEntry.location_id is stored."""
    locations = VenueConfig.list_locations(store_id)
    if len(locations) <= 1:
        return None, text, None
    command_part, branch_part = _split_branch_suffix(text)
    names = _join_or([l.branch_name for l in locations])
    if not branch_part:
        return None, text, f"Which branch — {names}? Add \"at <branch>\" to your message."
    matched = match_location(locations, branch_part)
    if matched is None:
        return None, text, f"Didn't recognise that branch. Choices: {names}."
    return matched.location_id, command_part, None


# ── queue commands ───────────────────────────────────────────────────────────

def format_queue(store_id: int) -> str:
    from app.agents.maitre_d.config import is_booking_enabled

    store = Store(store_id)
    rows = store.list_queue(status="waiting")
    off_notice = "" if is_booking_enabled(store_id) else "Booking is currently OFF.\n\n"
    if not rows:
        return off_notice + "The queue is empty."
    lines = [off_notice + "Live queue:"] if off_notice else ["Live queue:"]
    for e in rows:
        vip = " (VIP)" if e.is_vip else ""
        branch = f" — {e.branch_name}" if e.branch_name else ""
        lines.append(
            f"• {e.position}. #{e.queue_number} {e.name or e.phone}, "
            f"party {e.party_size}{branch}{vip}"
        )
    lines.append(
        "\nSay \"admit\" to seat the next person, \"remove <position>\" or "
        "\"add <position> <phone> <name>\" to adjust the line."
    )
    return "\n".join(lines)


def _restaurant_name(store_id: int) -> str:
    return VenueConfig.load(store_id).name


def _display_for(store_name: str, branch_name: str) -> str:
    return f"{store_name} ({branch_name})" if branch_name else store_name


def _notify_position_changes(store_id: int, store_name: str, shifted: list[QueueEntry]) -> None:
    """Every guest whose position moved (a queue mutation upstream of
    this) gets told their new spot in a short WhatsApp message."""
    if not shifted:
        return
    from app.core.outbound import send_from_store
    for e in shifted:
        send_from_store(
            store_id, e.phone,
            f"You're now #{e.position} in line at {_display_for(store_name, e.branch_name)}.",
        )


def admit_next_in_queue(store_id: int, rest: str = "") -> str:
    location_id, _, error = _resolve_queue_location(store_id, rest)
    if error:
        return error
    entry, moved_up = Store(store_id).admit_next(location_id, now=_venue_now(store_id, location_id))
    if entry is None:
        return "The queue is empty — nobody to admit."
    store_name = _restaurant_name(store_id)
    from app.core.outbound import send_from_store
    send_from_store(
        store_id, entry.phone,
        f"You're being seated now at {_display_for(store_name, entry.branch_name)}. Enjoy your meal!",
    )
    _notify_position_changes(store_id, store_name, moved_up)
    return f"Admitted #{entry.queue_number}: {entry.name or entry.phone}, party {entry.party_size}."


def remove_queue_position(store_id: int, rest: str) -> str:
    location_id, command_part, error = _resolve_queue_location(store_id, rest)
    if error:
        return error
    parts = command_part.split()
    if not parts or not parts[0].isdigit():
        return "Usage: remove <position> — e.g. \"remove 3\"."
    position = int(parts[0])
    entry, moved_up = Store(store_id).remove_at_position(location_id, position)
    if entry is None:
        return f"No one at position {position} right now. Say \"queue\" to see the live order."
    _notify_position_changes(store_id, _restaurant_name(store_id), moved_up)
    return (f"Removed #{entry.queue_number}: {entry.name or entry.phone} from "
            f"position {position}. The queue has moved up.")


def insert_queue_position(store_id: int, rest: str) -> str:
    location_id, command_part, error = _resolve_queue_location(store_id, rest)
    if error:
        return error
    parts = command_part.split(None, 2)
    if len(parts) < 3 or not parts[0].isdigit():
        return ("Usage: add <position> <phone> <name>[, party <N>] — e.g. "
                "\"add 2 +923001234567 Ali Khan, party 4\".")
    position = int(parts[0])
    phone = normalise_phone(parts[1])
    if not phone.startswith("+") or len(phone) < 8:
        return "That doesn't look like a phone number. Usage: add <position> <phone> <name>[, party <N>]"

    name_and_party = parts[2]
    party = 1
    m = re.search(r",?\s*party\s+(\d{1,2})\s*$", name_and_party, flags=re.IGNORECASE)
    name = name_and_party
    if m:
        party = int(m.group(1))
        name = name_and_party[:m.start()].strip().rstrip(",")
    name = name.strip()
    if not name:
        return "Usage: add <position> <phone> <name>[, party <N>]"

    branch_name = ""
    if location_id:
        loc = next(
            (l for l in VenueConfig.list_locations(store_id) if l.location_id == location_id), None,
        )
        branch_name = loc.branch_name if loc else ""

    entry = QueueEntry(phone=phone, name=name, party_size=party, location_id=location_id or 0, branch_name=branch_name)
    day_start = _venue_now(store_id, location_id).replace(hour=0, minute=0, second=0, microsecond=0)
    saved, pushed_back = Store(store_id).insert_at_position(entry, position, day_start=day_start)
    _notify_position_changes(store_id, _restaurant_name(store_id), pushed_back)
    return f"Added {name} (party {party}) at position {saved.position}, booking #{saved.queue_number}."


# ── VIPs / branches ──────────────────────────────────────────────────────────

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
        booking = "takes walk-ins" if loc.accepts_reservations else "delivery-only, no walk-ins"
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
    """Free-form Q&A grounded in the real live queue/VIP book, same
    single-LLM-call pattern as integrity/revenue/reputation's
    answer_question()."""
    try:
        from app.core.llm import get_client, get_model, nothink_kwargs
        from app.core.persona import staff_persona
        client = get_client()
    except Exception:
        return "Queue Q&A is unavailable right now, but you can still ask for *queue* or *vip list*."

    context = "\n\n".join([
        format_locations(store_id), format_queue(store_id), format_vips(store_id),
    ])
    system = (
        staff_persona("You answer staff questions about the live walk-in queue and VIP guests.", store_id)
        + "\nCite exact names, positions and queue numbers from the data; do not invent entries."
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
