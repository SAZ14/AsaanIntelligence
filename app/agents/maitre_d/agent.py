"""The Maitre D decision engine.

The LLM (in :mod:`nlu`) understands the guest; *this* module decides. Every
queue join/leave and VIP call is made by deterministic code here so it is
auditable and testable. Replies are composed plainly; delivery (WhatsApp
send, staff alerts) is the gateway layer's job, not this one's -- mirrors
how scout/reputation/revenue return text and app/gateway/main.py handles
dispatch.

This runs a live walk-in queue, not a date/time table-reservation system:
a store like Anatummy is walk-in first, so "book" means "join today's
queue and get a number" -- there's no table capacity, service windows,
deposits or no-show scoring to reason about any more. `store`/`config` are
the Postgres-backed, store_id-scoped versions in this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.agents.maitre_d.config import (
    VenueConfig, normalise_phone, match_location, _join_or,
    get_seated_grace_minutes, get_queue_stale_minutes,
)
from app.agents.maitre_d.models import Guest, QueueEntry
from app.agents.maitre_d.nlu import ParsedMessage, parse_message
from app.agents.maitre_d.store import Store


@dataclass
class MaitreDReply:
    text: str                                   # message to send back to the guest
    intent: str = "unknown"
    action: str = "noop"                        # queued | already_queued | cancelled | modified | need_info | info | already_seated | greeting | noop
    queue_number: int = 0
    position: int = 0
    is_vip: bool = False
    staff_alert: str = ""                       # internal note for the floor/manager
    outbound: list[tuple[str, str]] = field(default_factory=list)  # (phone, text) to others


@dataclass
class MaintenanceResult:
    """One housekeeping sweep's outcome -- see MaitreD.expire_stale_entries."""
    expired: int = 0
    outbound: list[tuple[str, str]] = field(default_factory=list)


class MaitreD:
    def __init__(
        self,
        store: Store,
        config: VenueConfig | None = None,
        client=None,
        now_fn=None,
        locations: list[VenueConfig] | None = None,
    ) -> None:
        self.store = store
        self.config = config or VenueConfig.load(store.store_id)
        self.client = client
        # Default clock is the *venue's* wall time, not the server's.
        self._now_fn = now_fn or self.config.now
        # All of this store's branches, if it has configured more than one
        # (see app.core.db.MaitreDLocation) -- an empty/single-item list
        # means "this store has one implicit/configured location", so the
        # booking flow never needs to ask which branch. Not auto-fetched
        # when a caller passes `locations` explicitly (tests construct
        # single-location scenarios without seeding MaitreDLocation rows).
        self.locations = (
            locations if locations is not None
            else VenueConfig.list_locations(store.store_id)
        )

    def _now(self) -> datetime:
        return self._now_fn()

    def _day_start(self) -> datetime:
        return self._now().replace(hour=0, minute=0, second=0, microsecond=0)

    def _conversation(self, phone: str) -> dict:
        """Read conversation state, dropping it if the flow has gone stale."""
        return self.store.get_conversation(
            phone, ttl_minutes=self.config.conversation_ttl_minutes, now=self._now()
        )

    def _save_conversation(self, phone: str, state: dict) -> None:
        self.store.set_conversation(phone, state, now=self._now())

    # ── entry point ──

    def handle_message(
        self, phone: str, text: str, profile_name: str = ""
    ) -> MaitreDReply:
        phone = normalise_phone(phone)
        vip = self.config.vip_for(phone)
        self._remember_guest(phone, profile_name, vip)

        parsed = parse_message(text, client=self.client)
        state = self._conversation(phone)
        flow = state.get("flow")

        if parsed.intent == "greeting" and not flow:
            return self._greet(phone, vip)
        if parsed.intent == "help":
            return self._info()
        if parsed.intent == "cancel":
            return self._leave_queue(phone)
        # Guarded by `flow != "book"`: a genuine mid-flow slot-filling
        # answer ("party of 5") during a FRESH booking must always
        # continue that flow, never get intercepted as a request to
        # modify some OTHER, already-existing entry -- see nlu.py's
        # _fallback_intent for why "party of N" classifies as "modify" at
        # all.
        if parsed.intent == "modify" and flow != "book":
            return self._modify_queue_entry(phone, parsed)
        if parsed.intent == "book" or flow == "book":
            return self._book_flow(phone, parsed, vip, profile_name)

        # Fall back gracefully. Deliberately doesn't say "type book" --
        # the queue-join trigger is meant to be scanned from the entrance
        # QR, not something the bot teaches every casual texter (see
        # gateway/customer.py's BOOKING_TRIGGER_PHRASE for why).
        if vip:
            return MaitreDReply(
                text=(f"Hello {vip.name}! To join today's queue, please scan the "
                      "QR code at our entrance."),
                intent="unknown", action="info", is_vip=True,
            )
        return MaitreDReply(
            text=(f"Hi! I'm the host at {self.config.name}. To join today's "
                  "queue, please scan the QR code at our entrance."),
            intent="unknown", action="info",
        )

    # ── greeting / info ──

    def _greet(self, phone: str, vip) -> MaitreDReply:
        if vip:
            return MaitreDReply(
                text=(f"Welcome back, {vip.name}! To join today's queue, please "
                      "scan the QR code at our entrance."),
                intent="greeting", action="greeting", is_vip=True,
                staff_alert=f"VIP {vip.name} ({vip.tier}) just messaged. {vip.notes}",
            )
        return MaitreDReply(
            text=(f"Hello and welcome to {self.config.name}! Scan the QR code "
                  "at our entrance to join today's queue and get a booking number."),
            intent="greeting", action="greeting",
        )

    def _info(self) -> MaitreDReply:
        return MaitreDReply(
            text=(f"{self.config.name} runs on a live walk-in queue — scan the "
                  "QR code at our entrance to join and get a booking number. "
                  "Say \"cancel\" any time to leave the queue."),
            intent="help", action="info",
        )

    # ── booking (joining the queue) ──

    def _book_flow(
        self, phone: str, parsed: ParsedMessage, vip, profile_name: str
    ) -> MaitreDReply:
        # A table's own QR code can't stop someone already seated from
        # scanning it and typing "book" -- WhatsApp lets a guest edit or
        # replace a QR's prefilled text, so nothing in the message itself
        # can tell "just walked in" apart from "already at table 5". The
        # only reliable signal is server-side: were they admitted recently
        # and not yet past a normal dining duration? If so, decline instead
        # of creating a second queue entry for the same visit.
        seated = self._seated_entry(phone)
        if seated is not None:
            self.store.clear_conversation(phone)
            location = self._resolve_location_config(seated.location_id)
            if location is not None:
                self.config = location
            return MaitreDReply(
                text=(f"You're already seated at {self.config.display_name()} — "
                      "enjoy your meal! Let us know if you need anything."),
                intent="book", action="already_seated", is_vip=bool(vip),
            )

        # A resent trigger phrase (no reply the first time, or two people
        # in the same group both scan) must not create a SECOND queue
        # entry for the same visit -- confirmed as a real gap: nothing
        # previously stopped this. Tell them their existing number instead.
        already_waiting = self.store.latest_waiting_entry_for(phone)
        if already_waiting is not None:
            self.store.clear_conversation(phone)
            location = self._resolve_location_config(already_waiting.location_id)
            if location is not None:
                self.config = location
            return MaitreDReply(
                text=(f"You're already in the queue at {self.config.display_name()} — "
                      f"your booking number is #{already_waiting.queue_number} "
                      f"(position {already_waiting.position}). Say \"cancel\" if you'd "
                      "like to leave the queue."),
                intent="book", action="already_queued",
                queue_number=already_waiting.queue_number, position=already_waiting.position,
                is_vip=bool(vip),
            )

        state = self._conversation(phone)
        slots = state.get("slots", {})
        prior_options = state.get("location_options", [])

        if parsed.party_size:
            slots["party_size"] = parsed.party_size
        if parsed.name:
            slots["name"] = parsed.name
        if parsed.special_requests:
            existing = slots.get("special_requests", "")
            slots["special_requests"] = ", ".join(
                p for p in [existing, parsed.special_requests] if p
            )

        # Fill name from VIP record / a previously saved guest record /
        # WhatsApp profile before ever asking for it -- "ask name only if
        # it's not already known for this number".
        if "name" not in slots:
            if vip and vip.name:
                slots["name"] = vip.name
            else:
                existing_guest = self.store.get_guest(phone)
                if existing_guest and existing_guest.name:
                    slots["name"] = existing_guest.name
                elif profile_name:
                    slots["name"] = profile_name

        # Multi-branch stores: try to auto-fill "location" from what the
        # guest just said before asking for it as a missing slot -- "table
        # for 4 at Bahria Town" shouldn't need a follow-up question just
        # because the branch happened to be named up front. A bare number
        # ("2") is only ever treated as a branch pick when we're actually
        # waiting on one (prior_options set from the last "which branch"
        # question) -- location is always resolved before party_size is
        # ever asked (see _missing_slot's ordering), so there's no turn
        # where a numeric reply could mean either.
        location_options: list[str] = []
        if len(self.locations) > 1:
            location_options = [l.branch_key for l in self.locations if l.accepts_reservations]
            if not slots.get("location"):
                matched = self._match_location_reply(parsed.raw, prior_options)
                if matched is not None:
                    if not matched.accepts_reservations:
                        others = _join_or(
                            [l.branch_name for l in self.locations if l.accepts_reservations]
                        )
                        return MaitreDReply(
                            text=(f"{matched.branch_name} is delivery-only and doesn't take "
                                  f"walk-ins. Would {others} work instead?"),
                            intent="book", action="need_info", is_vip=bool(vip),
                        )
                    slots["location"] = matched.branch_key

        # What's still missing?
        missing = self._missing_slot(slots)
        if missing:
            new_state = {"flow": "book", "slots": slots}
            if location_options:
                new_state["location_options"] = location_options
            self._save_conversation(phone, new_state)
            return MaitreDReply(
                text=self._ask_for(missing, vip, location_options), intent="book", action="need_info",
                is_vip=bool(vip),
            )

        # Multi-branch stores: switch to the resolved location for the reply.
        if len(self.locations) > 1:
            location = next(
                (l for l in self.locations if l.branch_key == slots.get("location")), None
            )
            if location is not None:
                self.config = location

        return self._join_queue(phone, slots, vip)

    def _join_queue(self, phone: str, slots: dict, vip) -> MaitreDReply:
        name = slots.get("name", "")
        party = int(slots.get("party_size") or 1)
        requests = slots.get("special_requests", "")

        # A name the guest just typed mid-flow ("it's Ahmed") only lived in
        # conversation slots until now -- persist it to the guest record so
        # a LATER, separate conversation from this number never has to ask
        # again (the whole point of "ask name only if not already saved").
        if name and not (vip and vip.name):
            self.store.upsert_guest(Guest(phone=phone, name=name, created_at=self._now()))

        entry = QueueEntry(
            phone=phone, name=name, party_size=party, special_requests=requests,
            location_id=self.config.location_id, branch_name=self.config.branch_name,
            is_vip=bool(vip), vip_tier=vip.tier if vip else "",
            created_at=self._now(),
        )
        entry = self.store.add_queue_entry(entry, day_start=self._day_start())
        self.store.clear_conversation(phone)

        staff_alert = ""
        if vip:
            staff_alert = (f"VIP {vip.name} ({vip.tier}) joined the queue "
                            f"(#{entry.queue_number}), party {party}. {vip.notes}")

        return MaitreDReply(
            text=(f"Hi {name}, your booking number at {self.config.display_name()} "
                  f"is: {entry.queue_number}"),
            intent="book", action="queued", queue_number=entry.queue_number,
            position=entry.position, is_vip=bool(vip), staff_alert=staff_alert,
        )

    # ── leaving the queue ──

    def _leave_queue(self, phone: str) -> MaitreDReply:
        entry = self.store.latest_waiting_entry_for(phone)
        if not entry:
            self.store.clear_conversation(phone)
            return MaitreDReply(
                text="I don't see you in today's queue right now. Anything else I can help with?",
                intent="cancel", action="noop",
            )
        location = self._resolve_location_config(entry.location_id)
        if location is not None:
            self.config = location
        _removed, moved_up = self.store.remove_by_id(entry.id, new_status="cancelled")
        self.store.clear_conversation(phone)
        reply = MaitreDReply(
            text=(f"Done — you've been removed from the queue at "
                  f"{self.config.display_name()} (you were #{entry.queue_number}). "
                  "Scan the QR code at our entrance any time to rejoin."),
            intent="cancel", action="cancelled", queue_number=entry.queue_number,
        )
        for e in moved_up:
            if e.phone:  # a staff-added walk-in may have no number on file
                reply.outbound.append((
                    e.phone, f"You're now #{e.position} in line at {self.config.display_name()}.",
                ))
        return reply

    # ── modifying an existing queue entry ──

    def _modify_queue_entry(self, phone: str, parsed: ParsedMessage) -> MaitreDReply:
        """"actually we're 5 now" / "put it under Bilal instead" -- updates
        the guest's EXISTING waiting entry in place rather than making
        them cancel and rejoin at the back of the line (which would also
        hand them a brand-new, higher queue number)."""
        entry = self.store.latest_waiting_entry_for(phone)
        if not entry:
            return MaitreDReply(
                text=("I don't see an active queue entry for you to update. "
                      "Scan the QR code at our entrance to join."),
                intent="modify", action="noop",
            )
        if not parsed.party_size and not parsed.name:
            return MaitreDReply(
                text="Sure — what would you like to update: your party size or the name on it?",
                intent="modify", action="need_info",
            )
        location = self._resolve_location_config(entry.location_id)
        if location is not None:
            self.config = location
        updated = self.store.update_waiting_entry(
            entry.id, party_size=parsed.party_size, name=parsed.name,
        )
        if updated is None:
            return MaitreDReply(
                text="That queue entry isn't there any more — say \"cancel\" or check with staff.",
                intent="modify", action="noop",
            )
        changes = []
        if parsed.party_size:
            changes.append(f"party of {updated.party_size}")
        if parsed.name:
            changes.append(f"name {updated.name}")
        return MaitreDReply(
            text=(f"Updated — you're still #{updated.queue_number} at "
                  f"{self.config.display_name()}, now {' and '.join(changes)}."),
            intent="modify", action="modified", queue_number=updated.queue_number,
            position=updated.position,
        )

    def _resolve_location_config(self, location_id: int) -> VenueConfig | None:
        """The specific location a given queue entry belongs to, looked up
        from self.locations. Returns None for single-location stores
        (location_id is meaningless there -- self.config is already
        correct) or if it can't be found."""
        if not location_id or len(self.locations) <= 1:
            return None
        return next((l for l in self.locations if l.location_id == location_id), None)

    def _seated_entry(self, phone: str) -> QueueEntry | None:
        """This phone's most recent admitted queue entry, if it's still
        within the "probably still dining" grace window (staff-configurable
        via "seated grace <N>", see config.get_seated_grace_minutes) -- None
        once that's passed, so a genuinely later visit is never
        permanently blocked from booking again."""
        entry = self.store.latest_admitted_entry_for(phone)
        if not entry or not entry.admitted_at:
            return None
        grace = get_seated_grace_minutes(self.store.store_id)
        if self._now() - entry.admitted_at > timedelta(minutes=grace):
            return None
        return entry

    # ── background maintenance (run on a timer / cron) ──

    def _each_location(self):
        """Locations to sweep, one at a time -- a multi-branch store's
        stale-queue timeout can differ per branch (staff can tune it store-
        wide via "queue timeout <N>", but the sweep still runs once per
        location so each branch's own queue is checked against its own
        entries). Single-location stores just get one pass with
        self.config unchanged."""
        return self.locations if len(self.locations) > 1 else [self.config]

    def expire_stale_entries(self) -> MaintenanceResult:
        """Housekeeping: release queue spots nobody's claimed within
        queue_stale_minutes (staff-configurable via "queue timeout <N>",
        see config.get_queue_stale_minutes) -- a "waiting" entry that's
        just sat there, never admitted nor cancelled, almost certainly
        means the guest left without saying anything. Otherwise it would
        occupy a position forever, until a staff member happened to
        notice and manually remove it. Safe to call on a timer (see
        run_maitre_d_maintenance_all)."""
        result = MaintenanceResult()
        stale_minutes = get_queue_stale_minutes(self.store.store_id)
        cutoff = self._now() - timedelta(minutes=stale_minutes)
        for location in self._each_location():
            self.config = location
            for entry in self.store.stale_waiting_entries(location.location_id, cutoff):
                removed, moved_up = self.store.remove_by_id(entry.id, new_status="expired")
                if removed is None:
                    continue
                result.expired += 1
                if removed.phone:
                    result.outbound.append((
                        removed.phone,
                        f"We haven't been able to seat you at {self.config.display_name()} "
                        "in time, so we've released your spot. Message us or scan the QR "
                        "code at our entrance again if you'd still like to join the queue.",
                    ))
                for e in moved_up:
                    if e.phone:
                        result.outbound.append((
                            e.phone, f"You're now #{e.position} in line at {self.config.display_name()}.",
                        ))
        return result

    # ── helpers ──

    def _missing_slot(self, slots: dict) -> str | None:
        if len(self.locations) > 1 and not slots.get("location"):
            return "location"
        if not slots.get("name"):
            return "name"
        if not slots.get("party_size"):
            return "party_size"
        return None

    def _match_location_reply(self, raw: str, prior_options: list[str]) -> VenueConfig | None:
        """A number (e.g. "2") picks the branch at that position in the
        numbered list we just showed -- deterministic, no name-typing
        ambiguity. Falls back to free-text name/key matching so a guest
        who names the branch unprompted (or ignores the number) still
        works."""
        stripped = raw.strip()
        if prior_options and stripped.isdigit():
            idx = int(stripped)
            if 1 <= idx <= len(prior_options):
                key = prior_options[idx - 1]
                found = next((l for l in self.locations if l.branch_key == key), None)
                if found is not None:
                    return found
        return match_location(self.locations, raw)

    def _ask_for(self, missing: str, vip, location_options: list[str] | None = None) -> str:
        if missing == "location":
            names = []
            for key in location_options or []:
                loc = next((l for l in self.locations if l.branch_key == key), None)
                if loc is not None:
                    names.append(loc.branch_name)
            numbered = "\n".join(f"{i}. {name}" for i, name in enumerate(names, start=1))
            return f"Which branch would you like?\n{numbered}\nJust reply with the number."
        if missing == "name":
            return "Happy to add you to the queue! What name should I put it under?"
        if missing == "party_size":
            return "And how many people in your party?"
        return "Could you tell me a little more?"

    def _remember_guest(self, phone: str, profile_name: str, vip) -> None:
        self.store.upsert_guest(Guest(
            phone=phone,
            name=(vip.name if vip and vip.name else profile_name),
            vip_tier=vip.tier if vip else "",
            vip_notes=vip.notes if vip else "",
            created_at=self._now(),
        ))


# ── Store-scoped factory (mirrors revenue's RevenueRegistry pattern) ──

def get_maitre_d(store_id: int) -> MaitreD:
    """Build a MaitreD for one store, wired to the shared ZAI client and
    the store's own venue config/VIP list. Not cached across calls (each
    method already opens its own SessionLocal, so this is cheap) -- callers
    that need one instance across several calls in a request can hold onto
    the return value themselves."""
    from app.core.llm import get_client

    try:
        client = get_client()
    except Exception:
        client = None
    return MaitreD(
        store=Store(store_id),
        config=VenueConfig.load(store_id),
        client=client,
    )


def run_maitre_d_maintenance_all() -> None:
    """Scheduled job (see scripts/run_server.py) -- one housekeeping tick
    per store: releases queue spots nobody's claimed within that store's
    queue_stale_minutes. The only housekeeping this walk-in queue model
    needs (no offer TTLs, no reminders, no deposit holds -- see this
    module's docstring); dispatches every resulting outbound message,
    mirroring what the guest-facing flow already does per-turn in
    gateway/customer.py."""
    import logging as _logging
    from app.core.db import SessionLocal, Store as StoreModel
    from app.core.outbound import send_from_store

    logger = _logging.getLogger(__name__)

    with SessionLocal() as db:
        store_ids = [s.id for s in db.query(StoreModel).all()]

    for store_id in store_ids:
        try:
            result = get_maitre_d(store_id).expire_stale_entries()
        except Exception as exc:
            logger.error("maitre_d.maintenance: store=%d failed: %s", store_id, exc)
            continue
        for phone, text in result.outbound:
            send_from_store(store_id, phone, text)
        if result.expired:
            logger.info(
                "maitre_d.maintenance: store=%d expired=%d", store_id, result.expired,
            )
