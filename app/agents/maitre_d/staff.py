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


# Sentinel distinguishing "no forced location given" from "forced to the
# store's single implicit location" (a real, valid value of None) -- see
# admit_next_in_queue/remove_queue_position/insert_queue_position/
# format_queue's forced_location_id parameter.
_UNSET = object()


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
        return None, text, f"Which branch ({names})? Add \"at <branch>\" to your message."
    matched = match_location(locations, branch_part)
    if matched is None:
        return None, text, f"Didn't recognise that branch. Choices: {names}."
    return matched.location_id, command_part, None


# ── queue commands ───────────────────────────────────────────────────────────

MAX_QUEUE_LISTING = 20  # a WhatsApp message showing every entry in a 30+-person rush would be unwieldy


def format_queue(store_id: int, forced_location_id=_UNSET) -> str:
    """forced_location_id UNSET (the default): show every branch -- what
    an owner or a single-location store sees. Any other value (including
    None, meaning the store's single implicit location) shows ONLY that
    one branch's line, regardless of what the caller's message text
    otherwise named -- what a branch-scoped staff/manager member sees
    (see internal.py's _staff_queue_scope, the only caller that passes
    this)."""
    from app.agents.maitre_d.config import is_booking_enabled

    store = Store(store_id)
    scoped = forced_location_id is not _UNSET
    location_id = forced_location_id if scoped else None
    rows = store.list_queue(status="waiting", location_id=location_id)
    off_notice = (
        "" if is_booking_enabled(store_id, forced_location_id if scoped else None)
        else "Booking is currently OFF.\n\n"
    )
    if not rows:
        return off_notice + "The queue is empty."

    # Grouped by branch so a multi-location store's combined listing never
    # shows two different branches both claiming "position 1" back to
    # back with nothing marking them as separate lines -- `rows` is
    # already ordered (location_id, position), so a plain dict preserves
    # that grouping without a second query. A single-location store has
    # exactly one (empty-string) key here, so it reads exactly as before:
    # no header, just the plain numbered list.
    sections: dict[str, list] = {}
    for e in rows:
        sections.setdefault(e.branch_name, []).append(e)

    lines = [off_notice + "Live queue:"] if off_notice else ["Live queue:"]
    total, shown = len(rows), 0
    for branch_name, entries in sections.items():
        if shown >= MAX_QUEUE_LISTING:
            break
        if branch_name:
            lines.append(f"\n{branch_name}:")
        for e in entries:
            if shown >= MAX_QUEUE_LISTING:
                break
            vip = " (VIP)" if e.is_vip else ""
            lines.append(f"• {e.position}. #{e.queue_number} {e.name or e.phone}, party {e.party_size}{vip}")
            shown += 1
    if total > shown:
        lines.append(f"\n...and {total - shown} more waiting.")
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
    this) gets told their new spot in a short WhatsApp message. A staff-
    added walk-in with no phone on file (see insert_queue_position) is
    silently skipped -- there's nowhere to send it."""
    if not shifted:
        return
    from app.core.outbound import send_from_store
    for e in shifted:
        if not e.phone:
            continue
        send_from_store(
            store_id, e.phone,
            f"You're now #{e.position} in line at {_display_for(store_name, e.branch_name)}.",
        )


def admit_next_in_queue(store_id: int, rest: str = "", forced_location_id=_UNSET) -> str:
    if forced_location_id is not _UNSET:
        location_id = forced_location_id
    else:
        location_id, _, error = _resolve_queue_location(store_id, rest)
        if error:
            return error
    entry, moved_up = Store(store_id).admit_next(location_id, now=_venue_now(store_id, location_id))
    if entry is None:
        return "The queue is empty, nobody to admit."
    store_name = _restaurant_name(store_id)
    if entry.phone:
        from app.core.outbound import send_from_store
        send_from_store(
            store_id, entry.phone,
            f"You're being seated now at {_display_for(store_name, entry.branch_name)}. "
            "Enjoy your meal! If you have any questions or need any kind of help, "
            "feel free to text us here!",
        )
    _notify_position_changes(store_id, store_name, moved_up)
    return f"Admitted #{entry.queue_number}: {entry.name or entry.phone}, party {entry.party_size}."


def remove_queue_position(store_id: int, rest: str, forced_location_id=_UNSET) -> str:
    if forced_location_id is not _UNSET:
        location_id, command_part = forced_location_id, rest.strip()
    else:
        location_id, command_part, error = _resolve_queue_location(store_id, rest)
        if error:
            return error
    parts = command_part.split()
    if not parts or not parts[0].isdigit():
        return "Usage: remove <position>, e.g. \"remove 3\"."
    position = int(parts[0])
    entry, moved_up = Store(store_id).remove_at_position(location_id, position)
    if entry is None:
        return f"No one at position {position} right now. Say \"queue\" to see the live order."
    _notify_position_changes(store_id, _restaurant_name(store_id), moved_up)
    return (f"Removed #{entry.queue_number}: {entry.name or entry.phone} from "
            f"position {position}. The queue has moved up.")


def _looks_like_phone(token: str) -> bool:
    return token.startswith("+") and token[1:].replace(" ", "").isdigit() and len(token) >= 8


def insert_queue_position(store_id: int, rest: str, forced_location_id=_UNSET) -> str:
    """"add <position> [<phone>] <name>[, party <N>]" -- phone is optional:
    a walk-in staff seat directly without collecting a number just won't
    get position-update texts (see _notify_position_changes)."""
    if forced_location_id is not _UNSET:
        location_id, command_part = forced_location_id, rest.strip()
    else:
        location_id, command_part, error = _resolve_queue_location(store_id, rest)
        if error:
            return error
    tokens = command_part.split()
    if not tokens or not tokens[0].isdigit():
        return ("Usage: add <position> [<phone>] <name>[, party <N>], e.g. "
                "\"add 2 +923001234567 Ali Khan, party 4\" or \"add 2 Ali Khan, party 4\" "
                "for a walk-in without a number.")
    position = int(tokens[0])
    remainder = " ".join(tokens[1:]).strip()
    if not remainder:
        return "Usage: add <position> [<phone>] <name>[, party <N>]"

    phone = ""
    rest_tokens = remainder.split(None, 1)
    if _looks_like_phone(rest_tokens[0]):
        phone = normalise_phone(rest_tokens[0])
        name_and_party = rest_tokens[1] if len(rest_tokens) > 1 else ""
    else:
        name_and_party = remainder

    party = 1
    m = re.search(r",?\s*party\s+(\d{1,2})\s*$", name_and_party, flags=re.IGNORECASE)
    name = name_and_party
    if m:
        party = int(m.group(1))
        name = name_and_party[:m.start()].strip().rstrip(",")
    name = name.strip()
    if not name:
        return "Usage: add <position> [<phone>] <name>[, party <N>]"

    branch_name = ""
    if location_id:
        loc = next(
            (l for l in VenueConfig.list_locations(store_id) if l.location_id == location_id), None,
        )
        branch_name = loc.branch_name if loc else ""

    now = _venue_now(store_id, location_id)
    # created_at must be the VENUE-local clock, not QueueEntry's own
    # default (server time, via pydantic's Field(default_factory=
    # datetime.now)) -- confirmed live: a walk-in inserted seconds ago
    # was immediately flagged as stale by expire_stale_entries() because
    # its created_at (server/UTC clock) read hours behind this store's
    # Asia/Karachi "now", making it look far older than it actually was.
    entry = QueueEntry(
        phone=phone, name=name, party_size=party, location_id=location_id or 0,
        branch_name=branch_name, created_at=now,
    )
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    saved, pushed_back = Store(store_id).insert_at_position(entry, position, day_start=day_start)
    _notify_position_changes(store_id, _restaurant_name(store_id), pushed_back)
    phone_note = "" if saved.phone else " (no phone on file, won't get text updates)"
    return f"Added {name} (party {party}) at position {saved.position}, booking #{saved.queue_number}{phone_note}."


# ── VIPs / branches ──────────────────────────────────────────────────────────

def format_vips(store_id: int) -> str:
    cfg = VenueConfig.load(store_id)
    if not cfg.vips:
        return "No VIPs on file yet. Add one: \"add vip +923001234567 Ayesha Khan, food critic\"."
    lines = ["VIP list:"]
    for phone, v in cfg.vips.items():
        notes = f", {v.notes}" if v.notes else ""
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
        addr = f", {loc.address}" if loc.address else ""
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

def answer_question(
    store_id: int, text: str, history: list[dict] | None = None, forced_location_id=_UNSET,
) -> str:
    """Free-form Q&A grounded in the real live queue/VIP book, same
    single-LLM-call pattern as integrity/revenue/reputation's
    answer_question(). forced_location_id (see format_queue) keeps a
    branch-scoped staff member's free-form questions grounded in ONLY
    their own branch's queue -- otherwise the LLM would happily answer
    from every branch's data even though the shorthand "queue" command
    never would."""
    try:
        from app.core.llm import get_client, get_model, nothink_kwargs
        from app.core.persona import staff_persona
        client = get_client()
    except Exception:
        return "Queue Q&A is unavailable right now, but you can still ask for *queue* or *vip list*."

    context = "\n\n".join([
        format_locations(store_id), format_queue(store_id, forced_location_id), format_vips(store_id),
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
