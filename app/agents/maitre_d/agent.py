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
from datetime import datetime

from app.agents.maitre_d.config import VenueConfig, normalise_phone, match_location, _join_or
from app.agents.maitre_d.models import Guest, QueueEntry
from app.agents.maitre_d.nlu import ParsedMessage, parse_message
from app.agents.maitre_d.store import Store


@dataclass
class MaitreDReply:
    text: str                                   # message to send back to the guest
    intent: str = "unknown"
    action: str = "noop"                        # queued | cancelled | need_info | info | greeting | noop
    queue_number: int = 0
    position: int = 0
    is_vip: bool = False
    staff_alert: str = ""                       # internal note for the floor/manager
    outbound: list[tuple[str, str]] = field(default_factory=list)  # (phone, text) to others


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
        if parsed.intent == "book" or flow == "book":
            return self._book_flow(phone, parsed, vip, profile_name)

        # Fall back gracefully.
        if vip:
            return MaitreDReply(
                text=(f"Hello {vip.name}! Say \"book\" any time and I'll add you "
                      "to today's queue."),
                intent="unknown", action="info", is_vip=True,
            )
        return MaitreDReply(
            text=(f"Hi! I'm the host at {self.config.name}. Say \"book\" and "
                  "I'll add you to today's live queue."),
            intent="unknown", action="info",
        )

    # ── greeting / info ──

    def _greet(self, phone: str, vip) -> MaitreDReply:
        if vip:
            return MaitreDReply(
                text=(f"Welcome back, {vip.name}! Say \"book\" any time and I'll "
                      "add you to today's queue."),
                intent="greeting", action="greeting", is_vip=True,
                staff_alert=f"VIP {vip.name} ({vip.tier}) just messaged. {vip.notes}",
            )
        return MaitreDReply(
            text=(f"Hello and welcome to {self.config.name}! Say \"book\" and "
                  "I'll add you to today's live queue and give you a booking number."),
            intent="greeting", action="greeting",
        )

    def _info(self) -> MaitreDReply:
        return MaitreDReply(
            text=(f"{self.config.name} runs on a live walk-in queue — say "
                  "\"book\" and I'll add you with a booking number. Say "
                  "\"cancel\" any time to leave the queue."),
            intent="help", action="info",
        )

    # ── booking (joining the queue) ──

    def _book_flow(
        self, phone: str, parsed: ParsedMessage, vip, profile_name: str
    ) -> MaitreDReply:
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
                  "Message \"book\" any time to rejoin."),
            intent="cancel", action="cancelled", queue_number=entry.queue_number,
        )
        for e in moved_up:
            reply.outbound.append((
                e.phone, f"You're now #{e.position} in line at {self.config.display_name()}.",
            ))
        return reply

    def _resolve_location_config(self, location_id: int) -> VenueConfig | None:
        """The specific location a given queue entry belongs to, looked up
        from self.locations. Returns None for single-location stores
        (location_id is meaningless there -- self.config is already
        correct) or if it can't be found."""
        if not location_id or len(self.locations) <= 1:
            return None
        return next((l for l in self.locations if l.location_id == location_id), None)

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
