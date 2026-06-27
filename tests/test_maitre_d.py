"""Property-based tests for the Maître d' agent.

No network / no Claude key: NLU uses the deterministic fallback parser, so the
suite exercises every booking/waitlist/no-show/VIP decision offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig, VipProfile
from app.maitre_d.models import Reservation
from app.maitre_d.noshow import assess_no_show
from app.maitre_d.nlu import parse_message
from app.maitre_d.payments import StubPaymentProvider
from app.maitre_d.store import Store
from app.maitre_d.whatsapp import validate_twilio_signature

# Reference "now": Monday 15 Jun 2026, 11:00. (Friday that week is the 19th.)
NOW = datetime(2026, 6, 15, 11, 0)
FRIDAY_8PM = datetime(2026, 6, 19, 20, 0)


# ── fixtures / helpers ──

def _md(config: VenueConfig | None = None) -> MaitreD:
    return MaitreD(
        store=Store(":memory:"),
        config=config or VenueConfig.load(),
        client=None,
        now_fn=lambda: NOW,
    )


def _md_clock(
    config: VenueConfig | None = None, start: datetime = NOW
) -> tuple[MaitreD, dict]:
    """A Maître d' with a mutable clock, so tests can advance time for sweeps."""
    clock = {"t": start}
    md = MaitreD(
        store=Store(":memory:"),
        config=config or VenueConfig.load(),
        client=None,
        now_fn=lambda: clock["t"],
    )
    return md, clock


def _tiny_config(tables: list[tuple[str, int]]) -> VenueConfig:
    cfg = VenueConfig.load()
    cfg.tables = tables
    cfg.vips = {}
    return cfg


# ── NLU (deterministic fallback) ──

class TestNLU:
    def test_party_size_variants(self):
        for text, expected in [
            ("table for 4 friday 8pm", 4),
            ("party of 6 please", 6),
            ("2 people tomorrow", 2),
            ("we are 5", 5),
        ]:
            assert parse_message(text, now=NOW).party_size == expected

    def test_weekday_resolves_to_next_occurrence(self):
        p = parse_message("friday 8pm", now=NOW)
        assert p.when == FRIDAY_8PM

    def test_tomorrow_and_time(self):
        p = parse_message("tomorrow at 7pm", now=NOW)
        assert p.when == datetime(2026, 6, 16, 19, 0)

    def test_passed_time_today_rolls_to_tomorrow(self):
        # 9am has already passed at NOW (11:00) and no date was given.
        p = parse_message("can I come at 9am", now=NOW)
        assert p.when == datetime(2026, 6, 16, 9, 0)

    def test_intents(self):
        assert parse_message("cancel my booking", now=NOW).intent == "cancel"
        assert parse_message("yes please", now=NOW).intent == "confirm"
        assert parse_message("no thanks", now=NOW).intent == "decline"
        assert parse_message("hi there", now=NOW).intent == "greeting"
        assert parse_message("table for 2", now=NOW).intent == "book"

    def test_bare_details_imply_booking(self):
        # No verb, but a party + time clearly means a booking.
        assert parse_message("4 people friday 8pm", now=NOW).intent == "book"

    def test_name_and_requests_extracted(self):
        p = parse_message("table for 2 friday 8pm, it's Omar, window please", now=NOW)
        assert p.name == "Omar"
        assert "window" in p.special_requests


# ── no-show scoring ──

class TestNoShowScoring:
    def _base(self, **kw):
        defaults = dict(
            when=FRIDAY_8PM, party_size=2, booked_at=NOW, is_vip=False, history=[],
        )
        defaults.update(kw)
        return assess_no_show(**defaults)

    def test_risk_within_bounds(self):
        a = self._base()
        assert 0.0 < a.risk < 1.0

    def test_prior_no_show_raises_risk(self):
        clean = self._base()
        flaky = self._base(history=[_res("no_show")])
        assert flaky.risk > clean.risk

    def test_vip_lowers_risk(self):
        normal = self._base()
        vip = self._base(is_vip=True)
        assert vip.risk < normal.risk

    def test_completed_visits_never_raise_risk(self):
        clean = self._base()
        loyal = self._base(history=[_res("completed"), _res("completed")])
        assert loyal.risk <= clean.risk

    def test_high_risk_requires_deposit(self):
        a = self._base(
            party_size=8, history=[_res("no_show"), _res("no_show")],
        )
        assert a.band == "high"
        assert a.require_deposit is True

    def test_vip_high_risk_not_asked_for_deposit(self):
        a = self._base(
            is_vip=True, party_size=8,
            history=[_res("no_show"), _res("no_show")],
        )
        assert a.require_deposit is False


# ── booking & availability ──

class TestBooking:
    def test_available_table_books(self):
        md = _md(_tiny_config([("A", 2)]))
        reply = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        assert reply.action == "booked"
        assert reply.reservation_id

    def test_full_slot_waitlists(self):
        md = _md(_tiny_config([("A", 2)]))
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        reply = md.handle_message("+9230002", "table for 2 friday 8pm, it's Ana")
        assert reply.action == "waitlisted"
        assert reply.waitlist_id

    def test_no_double_booking_same_table(self):
        md = _md(_tiny_config([("A", 2)]))
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        md.handle_message("+9230002", "table for 2 friday 8pm, it's Ana")
        confirmed = md.store.list_reservations(status="confirmed")
        assert len(confirmed) == 1  # only one party can hold table A at that time

    def test_non_overlapping_times_reuse_table(self):
        md = _md(_tiny_config([("A", 2)]))
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        # 9:30pm starts exactly when the 8pm turn (90 min) ends → no overlap.
        reply = md.handle_message("+9230002", "table for 2 friday 9:30pm, it's Ana")
        assert reply.action == "booked"

    def test_outside_service_window_asks_again(self):
        md = _md()
        reply = md.handle_message("+9230001", "table for 2 friday 4pm, it's Omar")
        assert reply.action == "need_info"

    def test_oversized_party_redirected(self):
        md = _md()
        reply = md.handle_message("+9230001", "table for 20 friday 8pm, it's Sam")
        assert reply.action == "info"
        assert "call" in reply.text.lower()

    def test_slot_filling_across_messages(self):
        md = _md()
        r1 = md.handle_message("+9230001", "I'd like to book a table", profile_name="Omar")
        assert r1.action == "need_info"
        r2 = md.handle_message("+9230001", "for 2")
        assert r2.action == "need_info"
        r3 = md.handle_message("+9230001", "friday 8pm")
        assert r3.action == "booked"  # name came from the WhatsApp profile

    def test_high_risk_booking_is_held_pending(self):
        md = _md(_tiny_config([("A", 2), ("B", 6)]))
        phone = "+9230009"
        # Seed two prior no-shows for this guest.
        for i in range(2):
            md.store.add_reservation(Reservation(
                reservation_id=f"old{i}", phone="+9230009", party_size=2,
                when=datetime(2026, 5, 1, 20, 0), status="no_show",
            ))
        reply = md.handle_message(phone, "table for 6 on 26 june at 8pm, it's Risky")
        assert reply.action == "pending"
        assert reply.no_show_band == "high"
        # Saying YES sends a payment link; the table stays pending until paid.
        confirm = md.handle_message(phone, "yes")
        assert confirm.action == "pending"
        res = md.store.get_reservation(confirm.reservation_id)
        assert res.payment_ref and res.status == "pending"
        # The gateway webhook then flips it to a confirmed booking.
        paid = md.handle_payment_webhook({"ref": res.payment_ref, "status": "paid"})
        assert paid is not None and paid.action == "booked"
        assert md.store.get_reservation(res.reservation_id).status == "confirmed"


# ── VIP recognition ──

class TestVIP:
    def test_vip_recognised_on_greeting(self):
        md = _md()  # default config includes the built-in VIP list
        reply = md.handle_message("+923001112222", "hi")
        assert reply.is_vip is True
        assert "Ayesha" in reply.text
        assert reply.staff_alert  # floor is alerted

    def test_vip_booking_flagged(self):
        md = _md()
        reply = md.handle_message("+923001112222", "table for 4 friday 8pm")
        assert reply.action == "booked"
        assert reply.is_vip is True
        res = md.store.get_reservation(reply.reservation_id)
        assert res.is_vip is True
        assert res.vip_tier == "vip"

    def test_vip_jumps_the_waitlist(self):
        cfg = _tiny_config([("A", 2)])
        cfg.vips = {"+923001112222": VipProfile(name="Ayesha", tier="vip")}
        md = _md(cfg)
        # Fill the only table with a normal guest, then two more waitlist.
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        md.handle_message("+9230002", "table for 2 friday 8pm, it's Normal")
        md.handle_message("+923001112222", "table for 2 friday 8pm")  # VIP waitlisted
        waiting = md.store.waiting_entries_near(FRIDAY_8PM)
        assert waiting[0].is_vip is True  # VIP sorted to the front of the queue


# ── cancellation & waitlist promotion ──

class TestCancellationAndPromotion:
    def test_cancel_marks_cancelled(self):
        md = _md(_tiny_config([("A", 2)]))
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        reply = md.handle_message("+9230001", "cancel")
        assert reply.action == "cancelled"
        assert md.store.get_reservation(booked.reservation_id).status == "cancelled"

    def test_cancel_promotes_waitlist(self):
        md = _md(_tiny_config([("A", 2)]))
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        md.handle_message("+9230002", "table for 2 friday 8pm, it's Ana")  # waitlisted
        cancel = md.handle_message("+9230001", "cancel")
        # Ana should be offered the freed table.
        assert any(phone == "+9230002" for phone, _ in cancel.outbound)
        # And accepting it confirms a new reservation.
        accept = md.handle_message("+9230002", "yes")
        assert accept.action == "booked"

    def test_cancel_with_no_booking_is_graceful(self):
        md = _md()
        reply = md.handle_message("+9230001", "cancel")
        assert reply.action == "noop"


# ── door / reservation lifecycle ──

class TestDoorLifecycle:
    def test_seat_complete_transitions(self):
        md = _md(_tiny_config([("A", 2)]))
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        seated = md.mark_seated(booked.reservation_id)
        assert seated.status == "seated"
        done = md.mark_completed(booked.reservation_id)
        assert done.status == "completed"

    def test_mark_no_show(self):
        md = _md(_tiny_config([("A", 2)]))
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        ns = md.mark_no_show(booked.reservation_id)
        assert ns.status == "no_show"

    def test_invalid_transition_rejected(self):
        md = _md(_tiny_config([("A", 2)]))
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        md.mark_completed(booked.reservation_id)
        # Already completed → can't be marked seated again.
        assert md.mark_seated(booked.reservation_id) is None

    def test_door_sweep_auto_completes_finished_visits(self):
        md, clock = _md_clock(_tiny_config([("A", 2)]), start=FRIDAY_8PM)
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        md.mark_seated(booked.reservation_id)
        clock["t"] = FRIDAY_8PM + timedelta(minutes=200)  # past the 90-min turn
        result = md.run_door_sweep()
        assert result.completed == 1
        assert md.store.get_reservation(booked.reservation_id).status == "completed"

    def test_door_sweep_flags_no_show(self):
        md, clock = _md_clock(_tiny_config([("A", 2)]), start=FRIDAY_8PM)
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        clock["t"] = FRIDAY_8PM + timedelta(hours=1)  # past the 30-min grace, unseated
        result = md.run_door_sweep()
        assert result.no_shows == 1
        assert md.store.get_reservation(booked.reservation_id).status == "no_show"
        assert result.staff_alerts

    def test_no_show_history_loop_is_closed(self):
        """A no-show recorded by the sweep raises the guest's next-booking risk."""
        md, clock = _md_clock(_tiny_config([("A", 2)]), start=FRIDAY_8PM)
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Flaky")
        clock["t"] = FRIDAY_8PM + timedelta(hours=1)
        md.run_door_sweep()  # → no_show recorded in history

        history = md.store.reservation_history_for("+9230001")
        when = datetime(2026, 6, 26, 20, 0)
        with_history = assess_no_show(
            when=when, party_size=2, booked_at=clock["t"], is_vip=False, history=history,
        )
        fresh = assess_no_show(
            when=when, party_size=2, booked_at=clock["t"], is_vip=False, history=[],
        )
        assert with_history.risk > fresh.risk


# ── waitlist offer expiry ──

class TestOfferExpiry:
    def test_stale_offer_rolls_to_next_in_line(self):
        md, clock = _md_clock(_tiny_config([("A", 2)]), start=NOW)
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")   # books A
        md.handle_message("+9230002", "table for 2 friday 8pm, it's Ana")    # waitlist 1
        md.handle_message("+9230003", "table for 2 friday 8pm, it's Bob")    # waitlist 2
        cancel = md.handle_message("+9230001", "cancel")
        assert any(p == "+9230002" for p, _ in cancel.outbound)              # Ana offered

        clock["t"] = NOW + timedelta(minutes=20)  # past the 15-min offer TTL
        result = md.expire_stale_offers()
        assert result.offers_expired == 1
        # Ana's offer lapsed and the freed table rolled to Bob.
        assert any(p == "+9230003" for p, _ in result.outbound)
        statuses = {w.name: w.status for w in md.store.list_waitlist()}
        assert statuses["Ana"] == "expired"
        assert statuses["Bob"] == "offered"


# ── reminders ──

class TestReminders:
    def test_reminder_sent_once(self):
        # Booking is ~24h out at NOW; reminder lead is 24h.
        start = FRIDAY_8PM - timedelta(hours=20)
        md, clock = _md_clock(_tiny_config([("A", 2)]), start=start)
        md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        first = md.send_due_reminders()
        assert first.reminders_sent == 1
        assert any(p == "+9230001" for p, _ in first.outbound)
        # A second pass must not re-send.
        assert md.send_due_reminders().reminders_sent == 0


# ── deposits / payments ──

class TestPayments:
    def _risky(self, md, phone="+9230009"):
        for i in range(2):
            md.store.add_reservation(Reservation(
                reservation_id=f"old{i}", phone=phone, party_size=2,
                when=datetime(2026, 5, 1, 20, 0), status="no_show",
            ))

    def test_deposit_link_then_webhook_confirms(self):
        md = _md(_tiny_config([("A", 2), ("B", 6)]))
        self._risky(md)
        hold = md.handle_message("+9230009", "table for 6 on 26 june 8pm, it's Risk")
        assert hold.action == "pending"
        link = md.handle_message("+9230009", "yes")
        res = md.store.get_reservation(link.reservation_id)
        assert res.status == "pending" and res.payment_ref
        reply = md.handle_payment_webhook({"ref": res.payment_ref, "status": "paid"})
        assert reply.action == "booked"
        assert md.store.get_reservation(res.reservation_id).status == "confirmed"
        assert md.store.get_reservation(res.reservation_id).deposit_paid is True

    def test_unpaid_webhook_is_ignored(self):
        md = _md(_tiny_config([("A", 2), ("B", 6)]))
        self._risky(md)
        md.handle_message("+9230009", "table for 6 on 26 june 8pm, it's Risk")
        link = md.handle_message("+9230009", "yes")
        res = md.store.get_reservation(link.reservation_id)
        assert md.handle_payment_webhook({"ref": res.payment_ref, "status": "failed"}) is None
        assert md.store.get_reservation(res.reservation_id).status == "pending"

    def test_decline_releases_hold(self):
        md = _md(_tiny_config([("A", 2), ("B", 6)]))
        self._risky(md)
        hold = md.handle_message("+9230009", "table for 6 on 26 june 8pm, it's Risk")
        decline = md.handle_message("+9230009", "no")
        assert decline.action == "cancelled"
        assert md.store.get_reservation(hold.reservation_id).status == "cancelled"

    def test_unpaid_hold_released_by_sweep(self):
        md, clock = _md_clock(_tiny_config([("A", 2), ("B", 6)]), start=NOW)
        for i in range(2):
            md.store.add_reservation(Reservation(
                reservation_id=f"old{i}", phone="+9230009", party_size=2,
                when=datetime(2026, 5, 1, 20, 0), status="no_show",
            ))
        hold = md.handle_message("+9230009", "table for 6 on 26 june 8pm, it's Risk")
        md.handle_message("+9230009", "yes")  # link sent, still pending
        clock["t"] = datetime(2026, 6, 26, 20, 1)  # past the booking time, unpaid
        result = md.run_door_sweep()
        assert result.deposits_expired == 1
        assert md.store.get_reservation(hold.reservation_id).status == "cancelled"

    def test_provider_creates_and_parses(self):
        prov = StubPaymentProvider()
        link = prov.create_checkout("res_x", 1000, "PKR")
        assert link.url.startswith("https://") and link.ref
        assert prov.parse_webhook({"ref": link.ref, "status": "paid"}) == (link.ref, True)
        assert prov.parse_webhook({"ref": link.ref, "status": "failed"})[1] is False


# ── modify ──

class TestModify:
    def test_change_time_frees_old_table(self):
        md = _md(_tiny_config([("A", 2)]))
        booked = md.handle_message("+9230009", "table for 2 friday 8pm, it's Zee")
        moved = md.handle_message("+9230009", "change it to friday 9:30pm")
        assert moved.action == "booked"
        assert moved.reservation_id != booked.reservation_id
        assert md.store.get_reservation(booked.reservation_id).status == "cancelled"
        new = md.store.get_reservation(moved.reservation_id)
        assert new.party_size == 2 and new.name == "Zee"   # carried over
        assert len(md.store.list_reservations(status="confirmed")) == 1

    def test_modify_without_booking_is_graceful(self):
        md = _md(_tiny_config([("A", 2)]))
        reply = md.handle_message("+9230009", "change it to friday 9pm")
        assert reply.action in ("need_info", "booked")


# ── conversation TTL ──

class TestConversationTTL:
    def test_stale_slot_fill_is_forgotten(self):
        md, clock = _md_clock(start=NOW)
        r1 = md.handle_message("+9230001", "I'd like a table", profile_name="Tom")
        assert r1.action == "need_info"
        clock["t"] = NOW + timedelta(minutes=200)  # past 180-min TTL
        assert md._conversation("+9230001") == {}


# ── Twilio signature ──

class TestSignature:
    def _sign(self, url, params, token):
        import base64, hashlib, hmac
        payload = url + "".join(k + str(params[k]) for k in sorted(params))
        return base64.b64encode(
            hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        ).decode()

    def test_valid_signature_accepted(self):
        url, params, token = "https://v/hook", {"B": "2", "A": "1"}, "tok"
        sig = self._sign(url, params, token)
        assert validate_twilio_signature(url, params, sig, token) is True

    def test_bad_signature_rejected(self):
        url, params, token = "https://v/hook", {"A": "1"}, "tok"
        assert validate_twilio_signature(url, params, "nope", token) is False

    def test_no_token_allows_dev_mode(self):
        assert validate_twilio_signature("https://v/hook", {"A": "1"}, "nope", "") is True


# ── maintenance aggregation ──

class TestMaintenance:
    def test_run_maintenance_aggregates(self):
        md, clock = _md_clock(_tiny_config([("A", 2)]), start=FRIDAY_8PM)
        booked = md.handle_message("+9230001", "table for 2 friday 8pm, it's Omar")
        clock["t"] = FRIDAY_8PM + timedelta(hours=1)  # unseated past grace
        result = md.run_maintenance()
        assert result.no_shows == 1
        assert md.store.get_reservation(booked.reservation_id).status == "no_show"


def _res(status: str) -> Reservation:
    return Reservation(
        reservation_id=f"h_{status}", phone="+9230001", party_size=2,
        when=datetime(2026, 1, 1, 20, 0), status=status,
    )
