"""Property-based tests for the Maître d' agent.

No network / no Claude key: NLU uses the deterministic fallback parser, so the
suite exercises every booking/waitlist/no-show/VIP decision offline.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig, VipProfile
from app.maitre_d.models import Reservation
from app.maitre_d.noshow import assess_no_show
from app.maitre_d.nlu import parse_message
from app.maitre_d.store import Store

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
        # Confirming the hold turns it into a real booking.
        confirm = md.handle_message(phone, "yes")
        assert confirm.action == "booked"


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


def _res(status: str) -> Reservation:
    return Reservation(
        reservation_id=f"h_{status}", phone="+9230001", party_size=2,
        when=datetime(2026, 1, 1, 20, 0), status=status,
    )
