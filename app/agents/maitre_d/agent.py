"""The Maitre D decision engine.

The LLM (in :mod:`nlu`) understands the guest; *this* module decides. Every
booking, waitlist, no-show and VIP call is made by deterministic code here
so it is auditable and testable. Replies are composed plainly; delivery
(WhatsApp send, staff alerts) is the gateway layer's job, not this one's --
mirrors how scout/reputation/revenue return text and app/gateway/main.py
handles dispatch.

Ported from the maitre-d-agent branch: logic is unchanged from there except
`client` is now the shared ZAI client (app.core.llm.get_client()) instead
of a raw anthropic.Anthropic instance, and `store`/`config` are the
Postgres-backed, store_id-scoped versions in this package instead of the
original single-tenant SQLite ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.agents.maitre_d.config import VenueConfig, normalise_phone
from app.agents.maitre_d.models import Guest, Reservation, WaitlistEntry
from app.agents.maitre_d.noshow import assess_no_show
from app.agents.maitre_d.nlu import ParsedMessage, parse_message


def _alnum(text: str) -> str:
    """Lowercase, strip everything but letters/digits -- so "F-8/2" and
    "at F-8/2," match regardless of hyphens, slashes or punctuation."""
    return re.sub(r"[^a-z0-9]", "", text.lower())
from app.agents.maitre_d.payments import PaymentProvider, StubPaymentProvider
from app.agents.maitre_d.store import Store


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


@dataclass
class MaintenanceResult:
    """Outcome of one background maintenance tick."""
    offers_expired: int = 0
    no_shows: int = 0
    completed: int = 0
    deposits_expired: int = 0
    reminders_sent: int = 0
    outbound: list[tuple[str, str]] = field(default_factory=list)
    staff_alerts: list[str] = field(default_factory=list)


class MaitreD:
    def __init__(
        self,
        store: Store,
        config: VenueConfig | None = None,
        client=None,
        now_fn=None,
        payments: PaymentProvider | None = None,
        locations: list[VenueConfig] | None = None,
    ) -> None:
        self.store = store
        self.config = config or VenueConfig.load(store.store_id)
        self.client = client
        # Default clock is the *venue's* wall time, not the server's.
        self._now_fn = now_fn or self.config.now
        self.payments = payments or StubPaymentProvider()
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

        parsed = parse_message(text, now=self._now(), client=self.client)
        state = self._conversation(phone)
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
            text=("Hi! I'm the Maitre D at "
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
        state = self._conversation(phone)
        slots = state.get("slots", {})
        # Carried across a modify: the old booking to replace once this one commits.
        replace_id = state.get("replace_reservation_id", "")

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

        # Multi-branch stores: try to auto-fill "location" from what the
        # guest just said before asking for it as a missing slot -- "table
        # for 4 at Bahria Town" shouldn't need a follow-up question just
        # because the branch happened to be named up front.
        if len(self.locations) > 1 and not slots.get("location"):
            matched = self._match_location(parsed.raw)
            if matched is not None:
                if not matched.accepts_reservations:
                    others = ", ".join(
                        l.branch_name for l in self.locations if l.accepts_reservations
                    )
                    return MaitreDReply(
                        text=(f"{matched.branch_name} is delivery-only and doesn't take "
                              f"table reservations. Would {others} work instead?"),
                        intent="book", action="need_info", is_vip=bool(vip),
                    )
                slots["location"] = matched.branch_key

        # What's still missing?
        missing = self._missing_slot(slots)
        if missing:
            new_state = {"flow": "book", "slots": slots}
            if replace_id:
                new_state["replace_reservation_id"] = replace_id
            self._save_conversation(phone, new_state)
            return MaitreDReply(
                text=self._ask_for(missing, vip), intent="book", action="need_info",
                is_vip=bool(vip),
            )

        # Multi-branch stores: switch to the resolved location's own
        # tables/service-windows/deposit rules for everything below.
        if len(self.locations) > 1:
            location = next(
                (l for l in self.locations if l.branch_key == slots.get("location")), None
            )
            if location is not None:
                self.config = location

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

        return self._try_seat(phone, slots, party, when, vip, replace_id=replace_id)

    def _try_seat(
        self, phone: str, slots: dict, party: int, when: datetime, vip,
        replace_id: str = "",
    ) -> MaitreDReply:
        name = slots.get("name", "")
        requests = slots.get("special_requests", "")
        # During a modify, the guest's own old booking must not block the new one.
        table_id = self._find_table(when, party, exclude_reservation_id=replace_id)

        if table_id is None:
            return self._add_to_waitlist(phone, name, party, when, vip, replace_id)

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
            location_id=self.config.location_id, branch_name=self.config.branch_name,
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
            # High-risk, non-VIP: hold pending a deposit to cut no-shows.
            self._save_conversation(phone, {
                "flow": "awaiting_confirm",
                "kind": "deposit",
                "reservation_id": res.reservation_id,
            })
            reply = MaitreDReply(
                text=(f"I can hold a table for {party} on {self._fmt_when(when)}. "
                      "As it's a busy slot we ask for a small deposit to secure it — "
                      "reply YES and I'll send a secure payment link, or NO to release it."),
                intent="book", action="pending", reservation_id=res.reservation_id,
                is_vip=False, no_show_band=assessment.band, staff_alert=staff_alert,
            )
            self._apply_replacement(replace_id, reply)
            return reply

        self.store.clear_conversation(phone)
        extras = ""
        if vip:
            extras = " We've noted your usual preferences and a warm welcome awaits."
        elif assessment.send_reminder:
            extras = " I'll send you a reminder the day before."
        if requests:
            extras += f" Noted: {requests}."
        reply = MaitreDReply(
            text=(f"You're booked, {name or 'see you soon'}! Table for {party} on "
                  f"{self._fmt_when(when)} at {self.config.display_name()}.{extras} "
                  "Reply CANCEL any time if your plans change."),
            intent="book", action="booked", reservation_id=res.reservation_id,
            is_vip=bool(vip), no_show_band=assessment.band, staff_alert=staff_alert,
        )
        self._apply_replacement(replace_id, reply)
        return reply

    def _apply_replacement(self, replace_id: str, reply: MaitreDReply) -> None:
        """Finalise a modify: cancel the superseded booking and free its slot."""
        if not replace_id:
            return
        old = self.store.get_reservation(replace_id)
        if old and old.status in ("pending", "confirmed", "seated"):
            self.store.update_reservation_status(replace_id, "cancelled")
            self._promote_waitlist(old.location_id, old.when, reply)

    def _resolve_location_config(self, location_id: int) -> VenueConfig | None:
        """The specific location a given reservation/waitlist entry
        belongs to, looked up from self.locations. Returns None for
        single-location stores (location_id is meaningless there --
        self.config is already correct) or if it can't be found."""
        if not location_id or len(self.locations) <= 1:
            return None
        return next((l for l in self.locations if l.location_id == location_id), None)

    def _add_to_waitlist(
        self, phone: str, name: str, party: int, when: datetime, vip,
        replace_id: str = "",
    ) -> MaitreDReply:
        entry = WaitlistEntry(
            waitlist_id="", phone=phone, name=name, party_size=party,
            requested_when=when, status="waiting", is_vip=bool(vip),
            vip_tier=vip.tier if vip else "", created_at=self._now(),
            location_id=self.config.location_id, branch_name=self.config.branch_name,
        )
        entry = self.store.add_waitlist(entry)
        self.store.clear_conversation(phone)

        position = "at the top of the list" if vip else "on the waitlist"
        staff_alert = ""
        if vip:
            staff_alert = (f"VIP {vip.name} ({vip.tier}) waitlisted for party {party} "
                           f"{self._fmt_when(when)} — try to make room. {vip.notes}")
        # On a modify we couldn't seat: leave the original booking untouched.
        kept = ""
        if replace_id and self.store.get_reservation(replace_id):
            kept = " Your existing booking still stands in the meantime."
        return MaitreDReply(
            text=(f"We're fully booked for {party} on {self._fmt_when(when)}, but "
                  f"I've put you {position} and I'll message the moment a table frees up.{kept}"),
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
        self._promote_waitlist(res.location_id, res.when, reply)
        return reply

    def _modify(
        self, phone: str, parsed: ParsedMessage, vip, profile_name: str
    ) -> MaitreDReply:
        old = self.store.latest_active_reservation_for(phone)
        if not old:
            # Nothing to change → treat it as a fresh booking request.
            return self._book_flow(phone, parsed, vip, profile_name)

        # Seed the booking flow with the existing details so a partial change
        # ("move it to 9pm", "make it 5 of us") keeps everything else. The old
        # booking is only cancelled once the new one actually commits, so a guest
        # never loses their table to a failed change.
        slots = {
            "party_size": old.party_size,
            "when": old.when.isoformat(),
            "name": old.name,
        }
        if old.special_requests:
            slots["special_requests"] = old.special_requests
        old_location = self._resolve_location_config(old.location_id)
        if old_location is not None:
            slots["location"] = old_location.branch_key
            self.config = old_location  # same branch's tables/rules apply to the replacement too
        self._save_conversation(phone, {
            "flow": "book",
            "slots": slots,
            "replace_reservation_id": old.reservation_id,
        })
        return self._book_flow(phone, parsed, vip, profile_name)

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
        location = self._resolve_location_config(res.location_id)
        if location is not None:
            self.config = location
        if intent == "confirm":
            # Issue a real checkout link; the table stays *pending* until the
            # payment webhook confirms it (see handle_payment_webhook).
            link = self.payments.create_checkout(
                res_id, self.config.deposit_amount, self.config.currency
            )
            self.store.set_payment_ref(res_id, link.ref)
            return MaitreDReply(
                text=(f"Great — to secure your table for {res.party_size} on "
                      f"{self._fmt_when(res.when)}, please pay the "
                      f"{self.config.currency} {self.config.deposit_amount} deposit here: "
                      f"{link.url} — your table is held until then and confirmed the "
                      "moment we receive it."),
                intent=intent, action="pending", reservation_id=res_id,
            )
        # declined
        self.store.update_reservation_status(res_id, "cancelled")
        reply = MaitreDReply(
            text="No problem, I've released that table. Message me any time to book.",
            intent=intent, action="cancelled", reservation_id=res_id,
        )
        self._promote_waitlist(res.location_id, res.when, reply)
        return reply

    # ── waitlist promotion ──

    def _promote_waitlist(self, location_id: int, freed_when: datetime, reply: MaitreDReply) -> None:
        """Offer a freed slot to the best waiting guest (VIP first, then FIFO)."""
        location = self._resolve_location_config(location_id)
        if location is not None:
            self.config = location  # this freed table's own branch, not whatever self.config last was
        candidates = self.store.waiting_entries_near(location_id, freed_when)
        for entry in candidates:
            table_id = self._find_table(entry.requested_when, entry.party_size)
            if table_id is None:
                continue
            self.store.update_waitlist_status(
                entry.waitlist_id, "offered", offered_at=self._now()
            )
            self._save_conversation(entry.phone, {
                "flow": "awaiting_waitlist_offer",
                "waitlist_id": entry.waitlist_id,
                "slots": {
                    "party_size": entry.party_size,
                    "when": entry.requested_when.isoformat(),
                    "name": entry.name,
                    "location_id": entry.location_id,
                },
            })
            msg = (f"Good news{', ' + entry.name if entry.name else ''}! A table for "
                   f"{entry.party_size} just opened on {self._fmt_when(entry.requested_when)} "
                   f"at {self.config.display_name()}. Reply YES in the next "
                   f"{self.config.offer_ttl_minutes} minutes and it's yours.")
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
        location_id = slots.get("location_id", 0)
        location = self._resolve_location_config(location_id)
        if location is not None:
            self.config = location
        self.store.clear_conversation(phone)
        if intent == "decline":
            self.store.update_waitlist_status(wl_id, "expired")
            reply = MaitreDReply(
                text="No worries — I've released it. You're welcome to book again any time.",
                intent=intent, action="cancelled", waitlist_id=wl_id,
            )
            when = datetime.fromisoformat(slots["when"]) if slots.get("when") else self._now()
            self._promote_waitlist(location_id, when, reply)  # pass it on to the next in line
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
            location_id=self.config.location_id, branch_name=self.config.branch_name,
            created_at=self._now(),
        ))
        return MaitreDReply(
            text=(f"Wonderful — confirmed! Table for {party} on {self._fmt_when(when)}. "
                  "See you then!"),
            intent=intent, action="booked", reservation_id=res.reservation_id,
            is_vip=bool(vip),
        )

    # ── door / reservation lifecycle (staff-driven) ──

    def _set_status(
        self, reservation_id: str, status: str, allowed: set[str]
    ) -> Reservation | None:
        res = self.store.get_reservation(reservation_id)
        if not res or res.status not in allowed:
            return None
        self.store.update_reservation_status(reservation_id, status)
        return self.store.get_reservation(reservation_id)

    def mark_seated(self, reservation_id: str) -> Reservation | None:
        """The guest has arrived and been seated."""
        return self._set_status(reservation_id, "seated", {"pending", "confirmed"})

    def mark_completed(self, reservation_id: str) -> Reservation | None:
        """The visit has finished (feeds future no-show scoring)."""
        return self._set_status(reservation_id, "completed", {"seated", "confirmed"})

    def mark_no_show(self, reservation_id: str) -> Reservation | None:
        """The guest never arrived (feeds future no-show scoring)."""
        res = self._set_status(reservation_id, "no_show", {"confirmed", "pending"})
        return res

    # ── payments (deposit confirmation) ──

    def handle_payment_webhook(self, payload: dict) -> MaitreDReply | None:
        """Process a gateway webhook → confirm the held table if it was paid."""
        ref, paid = self.payments.parse_webhook(payload)
        if not paid or not ref:
            return None
        return self.confirm_payment(payment_ref=ref)

    def confirm_payment(
        self, payment_ref: str = "", reservation_id: str = ""
    ) -> MaitreDReply | None:
        """Mark a deposit paid and flip the held table from pending → confirmed."""
        res = (self.store.get_reservation(reservation_id) if reservation_id
               else self.store.get_by_payment_ref(payment_ref))
        if not res:
            return None
        location = self._resolve_location_config(res.location_id)
        if location is not None:
            self.config = location
        self.store.mark_deposit_paid(res.reservation_id)
        if res.status == "pending":
            self.store.update_reservation_status(res.reservation_id, "confirmed")
        text = (f"Payment received — your table for {res.party_size} on "
                f"{self._fmt_when(res.when)} at {self.config.display_name()} is confirmed. "
                "See you soon!")
        return MaitreDReply(
            text=text, intent="confirm", action="booked",
            reservation_id=res.reservation_id, outbound=[(res.phone, text)],
        )

    # ── background maintenance (run on a timer / cron) ──

    def run_maintenance(self) -> MaintenanceResult:
        """One housekeeping tick: expire offers, sweep the door, send reminders.

        Pure of any inbound message — safe to call from a scheduler. Returns
        the outbound messages to dispatch plus any staff alerts and counts."""
        merged = MaintenanceResult()
        for part in (self.expire_stale_offers(), self.run_door_sweep(),
                     self.send_due_reminders()):
            merged.offers_expired += part.offers_expired
            merged.no_shows += part.no_shows
            merged.completed += part.completed
            merged.deposits_expired += part.deposits_expired
            merged.reminders_sent += part.reminders_sent
            merged.outbound.extend(part.outbound)
            merged.staff_alerts.extend(part.staff_alerts)
        return merged

    def _each_location(self):
        """Locations to sweep, one at a time -- a multi-branch store's
        offer/no-show/reminder timers can differ per branch, so maintenance
        runs once per location (switching self.config each time) rather
        than once for the whole store under a single arbitrary config.
        Single-location stores just get one pass with self.config
        unchanged, exactly as before."""
        return self.locations if len(self.locations) > 1 else [self.config]

    def expire_stale_offers(self) -> MaintenanceResult:
        """Release waitlist offers that went unanswered and roll them onward."""
        result = MaintenanceResult()
        now = self._now()
        for location in self._each_location():
            self.config = location
            cutoff = now - timedelta(minutes=self.config.offer_ttl_minutes)
            for entry in self.store.offered_entries_before(cutoff, location_id=location.location_id):
                self.store.update_waitlist_status(entry.waitlist_id, "expired")
                self.store.clear_conversation(entry.phone)
                result.offers_expired += 1
                result.outbound.append((
                    entry.phone,
                    "Your table offer has lapsed, but you're welcome to book again any time.",
                ))
                carrier = MaitreDReply(text="")
                self._promote_waitlist(entry.location_id, entry.requested_when, carrier)  # next in line
                result.outbound.extend(carrier.outbound)
                if carrier.staff_alert:
                    result.staff_alerts.append(carrier.staff_alert)
        return result

    def run_door_sweep(self) -> MaintenanceResult:
        """Auto-complete finished visits, flag no-shows, release unpaid holds."""
        result = MaintenanceResult()
        now = self._now()

        for location in self._each_location():
            self.config = location
            loc_id = location.location_id

            # Seated guests whose turn time has elapsed → completed.
            for r in self.store.reservations_due(("seated",), now, location_id=loc_id):
                turn = self.config.turn_time_for(r.party_size)
                if r.when + timedelta(minutes=turn) <= now:
                    self.store.update_reservation_status(r.reservation_id, "completed")
                    result.completed += 1

            # Confirmed but unseated past the grace window → no-show.
            ns_cutoff = now - timedelta(minutes=self.config.no_show_grace_minutes)
            for r in self.store.reservations_due(("confirmed",), ns_cutoff, location_id=loc_id):
                self.store.update_reservation_status(r.reservation_id, "no_show")
                result.no_shows += 1
                result.staff_alerts.append(
                    f"No-show: {r.name or r.phone}, party {r.party_size}, "
                    f"{self._fmt_when(r.when)} (table {r.table_id})."
                )

            # Pending deposit never paid by the booking time → release the hold.
            for r in self.store.reservations_due(("pending",), now, location_id=loc_id):
                self.store.update_reservation_status(r.reservation_id, "cancelled")
                self.store.clear_conversation(r.phone)
                result.deposits_expired += 1
                result.outbound.append((
                    r.phone,
                    "We couldn't confirm your deposit in time, so the table was released. "
                    "Message me to try again any time.",
                ))
                carrier = MaitreDReply(text="")
                self._promote_waitlist(r.location_id, r.when, carrier)
                result.outbound.extend(carrier.outbound)
                if carrier.staff_alert:
                    result.staff_alerts.append(carrier.staff_alert)
        return result

    def send_due_reminders(self) -> MaintenanceResult:
        """Send the day-before reminder for confirmed bookings coming up."""
        result = MaintenanceResult()
        now = self._now()
        for location in self._each_location():
            self.config = location
            for r in self.store.reminders_due(now, self.config.reminder_lead_hours, location_id=location.location_id):
                result.outbound.append((
                    r.phone,
                    f"Reminder: your table for {r.party_size} at {self.config.display_name()} is on "
                    f"{self._fmt_when(r.when)}. Reply CANCEL if your plans change — "
                    "otherwise we look forward to seeing you!",
                ))
                self.store.mark_reminder_sent(r.reservation_id)
                result.reminders_sent += 1
        return result

    # ── helpers ──

    def _find_table(
        self, when: datetime, party_size: int, exclude_reservation_id: str = ""
    ) -> str | None:
        turn = self.config.turn_time_for(party_size)
        overlapping = self.store.active_reservations_overlapping(
            self.config.location_id, when, turn
        )
        taken = {
            r.table_id for r in overlapping
            if r.table_id and r.reservation_id != exclude_reservation_id
        }
        for tid, _seats in self.config.tables_fitting(party_size):
            if tid not in taken:
                return tid
        return None

    def _missing_slot(self, slots: dict) -> str | None:
        if len(self.locations) > 1 and not slots.get("location"):
            return "location"
        if not slots.get("party_size"):
            return "party_size"
        if not slots.get("when") or slots.get("date_only"):
            return "when"
        if not slots.get("name"):
            return "name"
        return None

    def _match_location(self, text: str) -> VenueConfig | None:
        """Cheap keyword match against this store's own branch names/keys.
        Branch names are store-specific vocabulary (e.g. "Bahria Town",
        "F-8/2"), not something the generic NLU module (nlu.py) can know
        about, so this stays here rather than being taught to
        parse_message(). Punctuation-insensitive on both sides ("F-8/2"
        must match "at F-8/2" and "at f82" alike). Checks ALL locations,
        including ones that don't take reservations, so _book_flow can
        give a specific "that branch is delivery-only" answer instead of
        silently failing to match."""
        needle = _alnum(text)
        for loc in self.locations:
            if _alnum(loc.branch_key) in needle or _alnum(loc.branch_name) in needle:
                return loc
        return None

    def _ask_for(self, missing: str, vip) -> str:
        if missing == "location":
            names = ", ".join(l.branch_name for l in self.locations if l.accepts_reservations)
            return f"Which branch would you like — {names}?"
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
    per store: expires stale waitlist offers, sweeps the door (auto-
    complete/no-show/release unpaid holds), sends day-before reminders.
    Dispatches every resulting outbound message and staff alert, mirroring
    what the guest-facing flow already does per-turn in gateway/customer.py.

    Sequential per store, not parallel -- this is pure DB work (no external
    API fan-out like scout/reputation's Apify calls), so unlike those two
    there's no rate-limit reason to bound concurrency; sequential is simply
    simpler and this is cheap enough not to need more."""
    import logging as _logging
    from app.core.db import SessionLocal, Store as StoreModel
    from app.core.outbound import send_from_store, notify_staff

    logger = _logging.getLogger(__name__)

    with SessionLocal() as db:
        store_ids = [s.id for s in db.query(StoreModel).all()]

    for store_id in store_ids:
        try:
            result = get_maitre_d(store_id).run_maintenance()
        except Exception as exc:
            logger.error("maitre_d.maintenance: store=%d failed: %s", store_id, exc)
            continue
        for phone, text in result.outbound:
            send_from_store(store_id, phone, text)
        for alert in result.staff_alerts:
            notify_staff(store_id, f"[Reservations] {alert}")
        if any((result.offers_expired, result.no_shows, result.completed,
                result.deposits_expired, result.reminders_sent)):
            logger.info(
                "maitre_d.maintenance: store=%d offers_expired=%d no_shows=%d "
                "completed=%d deposits_expired=%d reminders_sent=%d",
                store_id, result.offers_expired, result.no_shows,
                result.completed, result.deposits_expired, result.reminders_sent,
            )
