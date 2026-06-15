"""The Maître d' decision engine.

Claude (in :mod:`nlu`) understands the guest; *this* module decides. Every
booking, waitlist, no-show and VIP call is made by deterministic code here so it
is auditable and testable. Replies are composed plainly and can be sent over any
channel (WhatsApp via :mod:`whatsapp`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from app.maitre_d.config import VenueConfig, _normalise_phone
from app.maitre_d.models import Guest, Reservation, WaitlistEntry
from app.maitre_d.noshow import assess_no_show
from app.maitre_d.nlu import ParsedMessage, parse_message
from app.maitre_d.store import Store


@dataclass
class MaitreDReply:
    text: str                                   # message to send back to the guest
    intent: str = "unknown"
    action: str = "noop"                        # booked | pending | waitlisted | cancelled | need_info | info | greeting | noop
    reservation_id: str = ""
    waitlist_id: str = ""
    is_vip: bool = False
    no_show_band: str = ""
    staff_alert: str = ""                       # internal note for the floor/manager
    outbound: list[tuple[str, str]] = field(default_factory=list)  # (phone, text) to others


class MaitreD:
    def __init__(
        self,
        store: Store,
        config: VenueConfig | None = None,
        client: anthropic.Anthropic | None = None,
        now_fn=None,
    ) -> None:
        self.store = store
        self.config = config or VenueConfig.load()
        self.client = client
        self._now_fn = now_fn or datetime.now

    def _now(self) -> datetime:
        return self._now_fn()

    # ── entry point ──

    def handle_message(
        self, phone: str, text: str, profile_name: str = ""
    ) -> MaitreDReply:
        phone = _normalise_phone(phone)
        vip = self.config.vip_for(phone)
        self._remember_guest(phone, profile_name, vip)

        parsed = parse_message(text, now=self._now(), client=self.client)
        state = self.store.get_conversation(phone)
        flow = state.get("flow")

        # Pending yes/no flows take priority over a fresh intent read.
        if flow == "awaiting_confirm" and parsed.intent in ("confirm", "decline"):
            return self._resolve_pending_confirm(phone, parsed.intent, state)
        if flow == "awaiting_waitlist_offer" and parsed.intent in ("confirm", "decline"):
            return self._resolve_waitlist_offer(phone, parsed.intent, state)

        if parsed.intent == "greeting" and not flow:
            return self._greet(phone, vip)
        if parsed.intent == "help":
            return self._info()
        if parsed.intent == "cancel":
            return self._cancel(phone)
        if parsed.intent == "modify":
            return self._modify(phone, parsed, vip, profile_name)
        if parsed.intent == "book" or flow == "book":
            return self._book_flow(phone, parsed, vip, profile_name)

        # Fall back gracefully.
        if vip:
            return MaitreDReply(
                text=(f"Hello {vip.name}! Would you like me to book a table? "
                      "Just tell me the day, time and how many."),
                intent="unknown", action="info", is_vip=True,
            )
        return MaitreDReply(
            text=("Hi! I'm the Maître d' at "
                  f"{self.config.name}. I can take a reservation — tell me the "
                  "date, time and party size, e.g. \"table for 4 Friday 8pm\"."),
            intent="unknown", action="info",
        )

    # ── greeting / info ──

    def _greet(self, phone: str, vip) -> MaitreDReply:
        if vip:
            return MaitreDReply(
                text=(f"Welcome back, {vip.name}! Lovely to hear from you. "
                      "Shall I arrange a table? Just say the day, time and party size."),
                intent="greeting", action="greeting", is_vip=True,
                staff_alert=f"VIP {vip.name} ({vip.tier}) just messaged. {vip.notes}",
            )
        return MaitreDReply(
            text=(f"Hello and welcome to {self.config.name}! I can book you a "
                  "table — tell me the date, time and how many people."),
            intent="greeting", action="greeting",
        )

    def _info(self) -> MaitreDReply:
        windows = ", ".join(
            f"{lbl} {oh:02d}:00–{lh:02d}:00"
            for lbl, oh, lh in self.config.service_windows
        )
        return MaitreDReply(
            text=(f"{self.config.name} takes reservations for: {windows}. "
                  "To book, tell me the date, time and party size."),
            intent="help", action="info",
        )

    # ── booking ──

    def _book_flow(
        self, phone: str, parsed: ParsedMessage, vip, profile_name: str
    ) -> MaitreDReply:
        state = self.store.get_conversation(phone)
        slots = state.get("slots", {})

        # Merge any newly extracted slots over what we already had.
        if parsed.party_size:
            slots["party_size"] = parsed.party_size
        if parsed.when:
            slots["when"] = parsed.when.isoformat()
            slots["date_only"] = parsed.date_only
        if parsed.name:
            slots["name"] = parsed.name
        if parsed.special_requests:
            existing = slots.get("special_requests", "")
            slots["special_requests"] = ", ".join(
                p for p in [existing, parsed.special_requests] if p
            )

        # Fill name from VIP record / WhatsApp profile if the guest never said one.
        if "name" not in slots:
            if vip and vip.name:
                slots["name"] = vip.name
            elif profile_name:
                slots["name"] = profile_name

        # What's still missing?
        missing = self._missing_slot(slots)
        if missing:
            self.store.set_conversation(phone, {"flow": "book", "slots": slots})
            return MaitreDReply(
                text=self._ask_for(missing, vip), intent="book", action="need_info",
                is_vip=bool(vip),
            )

        # All slots present → make the call.
        party = int(slots["party_size"])
        when = datetime.fromisoformat(slots["when"])

        if party > self.config.max_party_size:
            self.store.clear_conversation(phone)
            return MaitreDReply(
                text=(f"For parties over {self.config.max_party_size} we arrange "
                      "things personally — please call the restaurant and we'll "
                      "look after you."),
                intent="book", action="info", is_vip=bool(vip),
            )

        if self.config.hour_in_service_window(when.hour) is None:
            windows = ", ".join(
                f"{lbl} {oh:02d}:00–{lh:02d}:00"
                for lbl, oh, lh in self.config.service_windows
            )
            return MaitreDReply(
                text=(f"We don't seat at {self._fmt_time(when)}. Our service times "
                      f"are: {windows}. What time works?"),
                intent="book", action="need_info", is_vip=bool(vip),
            )

        return self._try_seat(phone, slots, party, when, vip)

    def _try_seat(
        self, phone: str, slots: dict, party: int, when: datetime, vip
    ) -> MaitreDReply:
        name = slots.get("name", "")
        requests = slots.get("special_requests", "")
        table_id = self._find_table(when, party)

        if table_id is None:
            return self._add_to_waitlist(phone, name, party, when, vip)

        history = self.store.reservation_history_for(phone)
        assessment = assess_no_show(
            when=when, party_size=party, booked_at=self._now(),
            is_vip=bool(vip), history=history,
        )

        status = "pending" if assessment.require_deposit else "confirmed"
        res = Reservation(
            reservation_id="", phone=phone, name=name, party_size=party,
            when=when, status=status, table_id=table_id, is_vip=bool(vip),
            vip_tier=vip.tier if vip else "", no_show_risk=assessment.risk,
            no_show_band=assessment.band,
            deposit_required=assessment.require_deposit,
            special_requests=requests, created_at=self._now(),
        )
        res = self.store.add_reservation(res)

        staff_alert = ""
        if vip:
            staff_alert = (f"VIP booking: {vip.name} ({vip.tier}), party {party}, "
                           f"{self._fmt_when(when)}, table {table_id}. {vip.notes}")
        elif assessment.band == "high":
            staff_alert = (f"High no-show risk ({assessment.risk:.0%}) for {name or phone}, "
                           f"party {party}, {self._fmt_when(when)}. "
                           f"Reasons: {', '.join(assessment.reasons)}.")

        if assessment.require_deposit:
            # High-risk, non-VIP: hold pending a deposit/confirmation to cut no-shows.
            self.store.set_conversation(phone, {
                "flow": "awaiting_confirm",
                "kind": "deposit",
                "reservation_id": res.reservation_id,
            })
            return MaitreDReply(
                text=(f"I can hold a table for {party} on {self._fmt_when(when)}. "
                      "As it's a busy slot we ask for a small deposit to secure it — "
                      "reply YES to confirm and I'll send the payment link, or NO to release it."),
                intent="book", action="pending", reservation_id=res.reservation_id,
                is_vip=False, no_show_band=assessment.band, staff_alert=staff_alert,
            )

        self.store.clear_conversation(phone)
        extras = ""
        if vip:
            extras = " We've noted your usual preferences and a warm welcome awaits."
        elif assessment.send_reminder:
            extras = " I'll send you a reminder the day before."
        if requests:
            extras += f" Noted: {requests}."
        return MaitreDReply(
            text=(f"You're booked, {name or 'see you soon'}! Table for {party} on "
                  f"{self._fmt_when(when)} at {self.config.name}.{extras} "
                  "Reply CANCEL any time if your plans change."),
            intent="book", action="booked", reservation_id=res.reservation_id,
            is_vip=bool(vip), no_show_band=assessment.band, staff_alert=staff_alert,
        )

    def _add_to_waitlist(
        self, phone: str, name: str, party: int, when: datetime, vip
    ) -> MaitreDReply:
        entry = WaitlistEntry(
            waitlist_id="", phone=phone, name=name, party_size=party,
            requested_when=when, status="waiting", is_vip=bool(vip),
            vip_tier=vip.tier if vip else "", created_at=self._now(),
        )
        entry = self.store.add_waitlist(entry)
        self.store.clear_conversation(phone)

        position = "at the top of the list" if vip else "on the waitlist"
        staff_alert = ""
        if vip:
            staff_alert = (f"VIP {vip.name} ({vip.tier}) waitlisted for party {party} "
                           f"{self._fmt_when(when)} — try to make room. {vip.notes}")
        return MaitreDReply(
            text=(f"We're fully booked for {party} on {self._fmt_when(when)}, but "
                  f"I've put you {position} and I'll message the moment a table frees up."),
            intent="book", action="waitlisted", waitlist_id=entry.waitlist_id,
            is_vip=bool(vip), staff_alert=staff_alert,
        )

    # ── cancel / modify ──

    def _cancel(self, phone: str) -> MaitreDReply:
        res = self.store.latest_active_reservation_for(phone)
        if not res:
            self.store.clear_conversation(phone)
            return MaitreDReply(
                text="I don't see an active booking under this number. Anything else I can help with?",
                intent="cancel", action="noop",
            )
        self.store.update_reservation_status(res.reservation_id, "cancelled")
        self.store.clear_conversation(phone)
        reply = MaitreDReply(
            text=(f"Done — your table for {res.party_size} on {self._fmt_when(res.when)} "
                  "is cancelled. We hope to see you another time!"),
            intent="cancel", action="cancelled", reservation_id=res.reservation_id,
        )
        self._promote_waitlist(res.when, reply)
        return reply

    def _modify(
        self, phone: str, parsed: ParsedMessage, vip, profile_name: str
    ) -> MaitreDReply:
        # Free the old table, then route into the booking flow with the new details.
        res = self.store.latest_active_reservation_for(phone)
        if res:
            self.store.update_reservation_status(res.reservation_id, "cancelled")
            self.store.clear_conversation(phone)
        reply = self._book_flow(phone, parsed, vip, profile_name)
        if res:
            self._promote_waitlist(res.when, reply)
        return reply

    # ── pending confirmation (deposit hold) ──

    def _resolve_pending_confirm(
        self, phone: str, intent: str, state: dict
    ) -> MaitreDReply:
        res_id = state.get("reservation_id", "")
        res = self.store.get_reservation(res_id)
        self.store.clear_conversation(phone)
        if not res:
            return MaitreDReply(text="That hold has expired — shall we start again?",
                                intent=intent, action="noop")
        if intent == "confirm":
            self.store.update_reservation_status(res_id, "confirmed")
            return MaitreDReply(
                text=(f"Lovely — your table for {res.party_size} on "
                      f"{self._fmt_when(res.when)} is confirmed. "
                      "Here's your secure deposit link: <payment-link>. See you soon!"),
                intent=intent, action="booked", reservation_id=res_id,
            )
        # declined
        self.store.update_reservation_status(res_id, "cancelled")
        reply = MaitreDReply(
            text="No problem, I've released that table. Message me any time to book.",
            intent=intent, action="cancelled", reservation_id=res_id,
        )
        self._promote_waitlist(res.when, reply)
        return reply

    # ── waitlist promotion ──

    def _promote_waitlist(self, freed_when: datetime, reply: MaitreDReply) -> None:
        """Offer a freed slot to the best waiting guest (VIP first, then FIFO)."""
        candidates = self.store.waiting_entries_near(freed_when)
        for entry in candidates:
            table_id = self._find_table(entry.requested_when, entry.party_size)
            if table_id is None:
                continue
            self.store.update_waitlist_status(
                entry.waitlist_id, "offered", offered_at=self._now()
            )
            self.store.set_conversation(entry.phone, {
                "flow": "awaiting_waitlist_offer",
                "waitlist_id": entry.waitlist_id,
                "slots": {
                    "party_size": entry.party_size,
                    "when": entry.requested_when.isoformat(),
                    "name": entry.name,
                },
            })
            msg = (f"Good news{', ' + entry.name if entry.name else ''}! A table for "
                   f"{entry.party_size} just opened on {self._fmt_when(entry.requested_when)} "
                   f"at {self.config.name}. Reply YES in the next 15 minutes and it's yours.")
            reply.outbound.append((entry.phone, msg))
            if entry.is_vip:
                reply.staff_alert = (reply.staff_alert + " " if reply.staff_alert else "") + \
                    f"VIP {entry.name} offered the freed table."
            return  # one freed table → one offer

    def _resolve_waitlist_offer(
        self, phone: str, intent: str, state: dict
    ) -> MaitreDReply:
        wl_id = state.get("waitlist_id", "")
        slots = state.get("slots", {})
        self.store.clear_conversation(phone)
        if intent == "decline":
            self.store.update_waitlist_status(wl_id, "expired")
            reply = MaitreDReply(
                text="No worries — I've released it. You're welcome to book again any time.",
                intent=intent, action="cancelled", waitlist_id=wl_id,
            )
            when = datetime.fromisoformat(slots["when"]) if slots.get("when") else self._now()
            self._promote_waitlist(when, reply)  # pass it on to the next in line
            return reply

        # confirm → convert to a real reservation
        party = int(slots["party_size"])
        when = datetime.fromisoformat(slots["when"])
        vip = self.config.vip_for(phone)
        table_id = self._find_table(when, party)
        if table_id is None:
            return MaitreDReply(
                text="So sorry — that table was just taken. You're still on the list and I'll keep trying.",
                intent=intent, action="waitlisted", waitlist_id=wl_id,
            )
        self.store.update_waitlist_status(wl_id, "converted")
        res = self.store.add_reservation(Reservation(
            reservation_id="", phone=phone, name=slots.get("name", ""),
            party_size=party, when=when, status="confirmed", table_id=table_id,
            is_vip=bool(vip), vip_tier=vip.tier if vip else "",
            created_at=self._now(),
        ))
        return MaitreDReply(
            text=(f"Wonderful — confirmed! Table for {party} on {self._fmt_when(when)}. "
                  "See you then!"),
            intent=intent, action="booked", reservation_id=res.reservation_id,
            is_vip=bool(vip),
        )

    # ── helpers ──

    def _find_table(self, when: datetime, party_size: int) -> str | None:
        turn = self.config.turn_time_for(party_size)
        overlapping = self.store.active_reservations_overlapping(when, turn)
        taken = {r.table_id for r in overlapping if r.table_id}
        for tid, _seats in self.config.tables_fitting(party_size):
            if tid not in taken:
                return tid
        return None

    def _missing_slot(self, slots: dict) -> str | None:
        if not slots.get("party_size"):
            return "party_size"
        if not slots.get("when") or slots.get("date_only"):
            return "when"
        if not slots.get("name"):
            return "name"
        return None

    def _ask_for(self, missing: str, vip) -> str:
        if missing == "party_size":
            return "Happy to help! How many people will be dining?"
        if missing == "when":
            return "Great — what day and time would you like?"
        if missing == "name":
            return "And what name should I put the booking under?"
        return "Could you tell me a little more?"

    def _remember_guest(self, phone: str, profile_name: str, vip) -> None:
        self.store.upsert_guest(Guest(
            phone=phone,
            name=(vip.name if vip and vip.name else profile_name),
            vip_tier=vip.tier if vip else "",
            vip_notes=vip.notes if vip else "",
            created_at=self._now(),
        ))

    def _fmt_when(self, when: datetime) -> str:
        return when.strftime("%A %d %b") + " at " + self._fmt_time(when)

    def _fmt_time(self, when: datetime) -> str:
        return when.strftime("%I:%M %p").lstrip("0")


# ── Convenience one-shot runner (mirrors run_<name>_agent convention) ──

def run_maitre_d_agent(
    transcript: list[tuple[str, str]],
    config: VenueConfig | None = None,
    client: anthropic.Anthropic | None = None,
    now: datetime | None = None,
    store: Store | None = None,
) -> list[MaitreDReply]:
    """Run a list of ``(phone, text)`` messages through a fresh Maître d'.

    Handy for demos and tests. Uses an in-memory store unless one is provided.
    """
    md = MaitreD(
        store=store or Store(":memory:"),
        config=config,
        client=client,
        now_fn=(lambda: now) if now else None,
    )
    return [md.handle_message(phone, text) for phone, text in transcript]
