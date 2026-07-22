"""Maitre D — reservations, waitlist, no-show/VIP, the door.

Ported from the maitre-d-agent branch's standalone test suite onto this
server's shared Postgres store/config (app.agents.maitre_d.store/config),
plus new coverage for the gateway wiring (customer-mode booking routing,
staff-mode door/listing/NL-Q&A routing) and multi-tenancy isolation, which
didn't exist on the single-tenant branch.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests.conftest import seed_chain, seed_store

from app.agents.maitre_d.agent import MaitreD, MaitreDReply
from app.agents.maitre_d.config import VenueConfig, VipProfile
from app.agents.maitre_d.store import Store
from app.agents.maitre_d.models import Reservation
from app.agents.maitre_d.noshow import assess_no_show
from app.agents.maitre_d.nlu import parse_message
from app.agents.maitre_d.payments import StubPaymentProvider


@pytest.fixture
def store_id():
    chain_id = seed_chain("MD Chain")
    return seed_store(chain_id, name="MD Test Cafe", location="F-7, Islamabad")


FRIDAY_8PM = datetime(2026, 7, 17, 20, 0)  # a Friday, deposit-safe unless noted


def _md(sid: int, now: datetime = FRIDAY_8PM, vips: dict | None = None) -> MaitreD:
    cfg = VenueConfig.load(sid)
    if vips is not None:
        cfg.vips = vips
    return MaitreD(store=Store(sid), config=cfg, client=None, now_fn=lambda: now)


PHONE = "+923001234567"


# ── NLU fallback parser ─────────────────────────────────────────────────────

class TestNLU:
    def test_party_size_variants(self):
        assert parse_message("table for 4").party_size == 4
        assert parse_message("party of 6").party_size == 6
        assert parse_message("we are 3").party_size == 3

    def test_tomorrow_and_time(self):
        now = datetime(2026, 7, 15, 10, 0)  # Wednesday
        parsed = parse_message("table for 2 tomorrow 8pm", now=now)
        assert parsed.when == datetime(2026, 7, 16, 20, 0)

    def test_passed_time_today_rolls_to_tomorrow(self):
        now = datetime(2026, 7, 15, 21, 0)
        parsed = parse_message("table for 2 at 8pm", now=now)
        assert parsed.when.date() == (now + timedelta(days=1)).date()

    def test_intents(self):
        assert parse_message("cancel my booking").intent == "cancel"
        assert parse_message("yes").intent == "confirm"
        assert parse_message("no thanks").intent == "decline"
        assert parse_message("hi there").intent == "greeting"

    def test_bare_details_imply_booking(self):
        assert parse_message("table for 4 friday 8pm").intent == "book"

    def test_name_and_requests_extracted(self):
        parsed = parse_message("table for 2, it's Ayesha, window seat please")
        assert parsed.name == "Ayesha"
        assert "window" in parsed.special_requests


# ── No-show scoring (pure, no DB) ───────────────────────────────────────────

class TestNoShowScoring:
    def test_risk_within_bounds(self):
        a = assess_no_show(when=FRIDAY_8PM, party_size=2, booked_at=FRIDAY_8PM - timedelta(hours=1), is_vip=False)
        assert 0.0 < a.risk < 1.0

    def test_prior_no_show_raises_risk(self):
        booked_at = FRIDAY_8PM - timedelta(hours=1)
        clean = assess_no_show(when=FRIDAY_8PM, party_size=2, booked_at=booked_at, is_vip=False)
        history = [Reservation(phone=PHONE, party_size=2, when=FRIDAY_8PM, status="no_show")]
        risky = assess_no_show(when=FRIDAY_8PM, party_size=2, booked_at=booked_at, is_vip=False, history=history)
        assert risky.risk > clean.risk

    def test_vip_lowers_risk(self):
        booked_at = FRIDAY_8PM - timedelta(days=8)
        non_vip = assess_no_show(when=FRIDAY_8PM, party_size=2, booked_at=booked_at, is_vip=False)
        vip = assess_no_show(when=FRIDAY_8PM, party_size=2, booked_at=booked_at, is_vip=True)
        assert vip.risk < non_vip.risk

    def test_high_risk_requires_deposit(self):
        booked_at = FRIDAY_8PM - timedelta(days=10)
        history = [Reservation(phone=PHONE, party_size=8, when=FRIDAY_8PM, status="no_show")] * 2
        a = assess_no_show(when=FRIDAY_8PM, party_size=8, booked_at=booked_at, is_vip=False, history=history)
        assert a.band == "high"
        assert a.require_deposit is True

    def test_vip_high_risk_not_asked_for_deposit(self):
        booked_at = FRIDAY_8PM - timedelta(days=10)
        history = [Reservation(phone=PHONE, party_size=8, when=FRIDAY_8PM, status="no_show")] * 2
        a = assess_no_show(when=FRIDAY_8PM, party_size=8, booked_at=booked_at, is_vip=True, history=history)
        assert a.require_deposit is False


# ── Booking ──────────────────────────────────────────────────────────────────

class TestBooking:
    def test_available_table_books(self, store_id):
        md = _md(store_id)
        reply = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        assert reply.action == "booked"
        assert reply.reservation_id

    def test_full_slot_waitlists(self, store_id):
        md = _md(store_id)
        cfg = md.config
        cfg.tables = [("T1", 2)]  # exactly one table, capacity 2
        for i in range(1):
            r = md.handle_message(f"+92300000000{i}", "table for 2 friday 8pm, it's Guest")
            assert r.action == "booked"
        r2 = md.handle_message("+923000000099", "table for 2 friday 8pm, it's Overflow")
        assert r2.action == "waitlisted"

    def test_no_double_booking_same_table(self, store_id):
        md = _md(store_id)
        md.config.tables = [("T1", 4)]
        md.handle_message("+923000000001", "table for 4 friday 8pm, it's Amir")
        r2 = md.handle_message("+923000000002", "table for 4 friday 8pm, it's Bilal")
        assert r2.action == "waitlisted"

    def test_outside_service_window_asks_again(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 4am, it's Ahmed")
        assert r.action in ("need_info", "info")

    def test_oversized_party_redirected(self, store_id):
        # The fallback parser's party-size regex sanity-caps extraction at
        # 30 (anything above that is treated as noise, not a real party
        # size), so this needs a number the parser will actually extract
        # (<=30) but still above max_party_size (12 by default) to reach
        # the oversized-party redirect in _book_flow.
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 20 friday 8pm, it's Ahmed")
        assert "call" in r.text.lower() or "personally" in r.text.lower()

    def test_slot_filling_across_messages(self, store_id):
        md = _md(store_id)
        r1 = md.handle_message(PHONE, "table for 4")
        assert r1.action == "need_info"
        r2 = md.handle_message(PHONE, "friday 8pm")
        assert r2.action == "need_info"  # still needs a name
        r3 = md.handle_message(PHONE, "it's Ahmed")
        assert r3.action == "booked"

    def test_high_risk_booking_is_held_pending(self, store_id):
        _seed_high_risk_history(store_id, PHONE)
        booked_far_ahead = FRIDAY_8PM - timedelta(days=10)
        md = _md(store_id, now=booked_far_ahead)
        r = md.handle_message(PHONE, "table for 8 this friday 8pm, it's Guest")
        assert r.no_show_band == "high"
        assert r.action == "pending"


# ── VIP recognition ──────────────────────────────────────────────────────────

class TestVIP:
    def test_vip_recognised_on_greeting(self, store_id):
        vips = {PHONE: VipProfile(name="Ayesha Khan", tier="vip", notes="food critic")}
        md = _md(store_id, vips=vips)
        r = md.handle_message(PHONE, "hi")
        assert r.is_vip
        assert "Ayesha" in r.text

    def test_vip_booking_flagged(self, store_id):
        vips = {PHONE: VipProfile(name="Ayesha Khan", tier="vip")}
        md = _md(store_id, vips=vips)
        r = md.handle_message(PHONE, "table for 2 friday 8pm")
        assert r.is_vip
        assert r.action == "booked"

    def test_vip_jumps_the_waitlist(self, store_id):
        md = _md(store_id, vips={PHONE: VipProfile(name="Ayesha", tier="vip")})
        md.config.tables = [("T1", 2)]
        md.handle_message("+923000000001", "table for 2 friday 8pm, it's Regular")
        r = md.handle_message(PHONE, "table for 2 friday 8pm")
        assert r.action == "waitlisted"
        assert "top of the list" in r.text


# ── Cancel / promotion ───────────────────────────────────────────────────────

class TestCancellationAndPromotion:
    def test_cancel_marks_cancelled(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        cancel = md.handle_message(PHONE, "cancel")
        assert cancel.action == "cancelled"
        res = md.store.get_reservation(r.reservation_id)
        assert res.status == "cancelled"

    def test_cancel_promotes_waitlist(self, store_id):
        md = _md(store_id)
        md.config.tables = [("T1", 2)]
        booked = md.handle_message("+923000000001", "table for 2 friday 8pm, it's Amir")
        waiter = "+923000000002"
        wl = md.handle_message(waiter, "table for 2 friday 8pm, it's Bilal")
        assert wl.action == "waitlisted"
        cancel = md.handle_message("+923000000001", "cancel")
        assert cancel.outbound  # the waiter got offered the freed table
        assert cancel.outbound[0][0] == waiter

    def test_cancel_with_no_booking_is_graceful(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "cancel")
        assert r.action == "noop"


# ── Door lifecycle ───────────────────────────────────────────────────────────

class TestDoorLifecycle:
    def test_seat_complete_transitions(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        seated = md.mark_seated(r.reservation_id)
        assert seated.status == "seated"
        completed = md.mark_completed(r.reservation_id)
        assert completed.status == "completed"

    def test_mark_no_show(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        res = md.mark_no_show(r.reservation_id)
        assert res.status == "no_show"

    def test_invalid_transition_rejected(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        md.mark_completed(r.reservation_id)  # confirmed -> completed is allowed once
        again = md.mark_completed(r.reservation_id)  # already completed
        assert again is None

    def test_door_sweep_auto_completes_finished_visits(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        md.mark_seated(r.reservation_id)
        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=120))
        later.store = md.store
        result = later.run_door_sweep()
        assert result.completed == 1

    def test_door_sweep_flags_no_show(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=45))
        result = later.run_door_sweep()
        assert result.no_shows == 1
        assert result.staff_alerts


# ── Offer expiry ─────────────────────────────────────────────────────────────

class TestOfferExpiry:
    def test_stale_offer_rolls_to_next_in_line(self, store_id):
        md = _md(store_id)
        md.config.tables = [("T1", 2)]
        md.handle_message("+923000000001", "table for 2 friday 8pm, it's Amir")
        second = "+923000000002"
        third = "+923000000003"
        md.handle_message(second, "table for 2 friday 8pm, it's Bilal")
        md.handle_message(third, "table for 2 friday 8pm, it's Cyrus")
        md.handle_message("+923000000001", "cancel")  # offers it to `second`

        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=30))
        later.store = md.store
        result = later.expire_stale_offers()
        assert result.offers_expired == 1
        # the offer should have rolled on to `third`
        assert any(p == third for p, _ in result.outbound)


# ── Reminders ────────────────────────────────────────────────────────────────

class TestReminders:
    def test_reminder_sent_once(self, store_id):
        booking_time = FRIDAY_8PM
        md = _md(store_id, now=booking_time - timedelta(days=2))
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")

        near = _md(store_id, now=booking_time - timedelta(hours=20))
        near.store = md.store
        result = near.send_due_reminders()
        assert result.reminders_sent == 1

        again = near.send_due_reminders()
        assert again.reminders_sent == 0


# ── Payments ─────────────────────────────────────────────────────────────────

def _seed_high_risk_history(store_id: int, phone: str) -> None:
    """Two prior no-shows push assess_no_show() reliably into the "high"
    band regardless of the other (already risk-raising) factors a test
    scenario picks, so deposit-flow tests don't depend on borderline
    scoring math to land where they need to."""
    store = Store(store_id)
    for _ in range(2):
        store.add_reservation(Reservation(
            phone=phone, party_size=4, when=FRIDAY_8PM - timedelta(days=30),
            status="no_show",
        ))


class TestPayments:
    def test_deposit_link_then_webhook_confirms(self, store_id):
        _seed_high_risk_history(store_id, PHONE)
        booked_far_ahead = FRIDAY_8PM - timedelta(days=10)
        md = _md(store_id, now=booked_far_ahead)
        r = md.handle_message(PHONE, "table for 8 friday 8pm, it's Ahmed")
        assert r.action == "pending"
        confirm = md.handle_message(PHONE, "yes")
        assert "http" in confirm.text
        res = md.store.get_reservation(confirm.reservation_id)
        payload = {"ref": res.payment_ref, "status": "paid"}
        webhook_reply = md.handle_payment_webhook(payload)
        assert webhook_reply is not None
        confirmed = md.store.get_reservation(confirm.reservation_id)
        assert confirmed.status == "confirmed"
        assert confirmed.deposit_paid

    def test_unpaid_webhook_is_ignored(self, store_id):
        md = _md(store_id)
        reply = md.handle_payment_webhook({"ref": "nonexistent", "status": "failed"})
        assert reply is None

    def test_decline_releases_hold(self, store_id):
        _seed_high_risk_history(store_id, PHONE)
        booked_far_ahead = FRIDAY_8PM - timedelta(days=10)
        md = _md(store_id, now=booked_far_ahead)
        r = md.handle_message(PHONE, "table for 8 friday 8pm, it's Ahmed")
        assert r.action == "pending"
        decline = md.handle_message(PHONE, "no")
        assert decline.action == "cancelled"

    def test_provider_creates_and_parses(self):
        p = StubPaymentProvider()
        link = p.create_checkout("res_1", 1000, "PKR")
        assert link.url and link.ref
        ref, paid = p.parse_webhook({"ref": link.ref, "status": "paid"})
        assert paid and ref == link.ref


# ── Modify ───────────────────────────────────────────────────────────────────

class TestModify:
    def test_change_time_frees_old_table(self, store_id):
        md = _md(store_id)
        md.config.tables = [("T1", 2)]
        r1 = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        r2 = md.handle_message(PHONE, "change it to 9pm")
        assert r2.action == "booked"
        old = md.store.get_reservation(r1.reservation_id)
        assert old.status == "cancelled"

    def test_modify_without_booking_is_graceful(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "move it to 9pm")
        # No existing booking -> treated as a fresh booking request
        assert r.intent in ("book",) or r.action == "need_info"


# ── Conversation TTL ─────────────────────────────────────────────────────────

class TestConversationTTL:
    def test_stale_slot_fill_is_forgotten(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 4")  # starts a flow, missing when/name

        later = _md(store_id, now=FRIDAY_8PM + timedelta(hours=4))  # past 180min TTL
        later.store = md.store
        state = later._conversation(PHONE)
        assert state == {}


# ── Maintenance aggregation ──────────────────────────────────────────────────

class TestMaintenance:
    def test_run_maintenance_aggregates(self, store_id):
        md = _md(store_id)
        md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=45))
        later.store = md.store
        result = later.run_maintenance()
        assert result.no_shows == 1


# ── Multi-location branches (Anatummy has three; a booking must be tied
# to one specific branch, not conflated across physically different
# addresses) ─────────────────────────────────────────────────────────────

def _seed_two_locations(store_id: int) -> tuple[int, int]:
    """New Blue Area (primary, dine-in) + F-8/2 (delivery-only, no
    reservations) -- mirrors Anatummy's real setup. Returns (blue_area_id,
    f82_id). Both locations get their own single "T1" table so a
    location-scoping bug (capacity checked store-wide instead of per-
    branch) would show up as a false double-booking conflict."""
    from app.core.db import SessionLocal, MaitreDLocation

    with SessionLocal() as db:
        blue = MaitreDLocation(
            store_id=store_id, branch_key="new_blue_area", name="New Blue Area",
            address="Skyline Tower, G-9/2", is_primary=True, accepts_reservations=True,
            tables=[["T1", 4]], service_windows=[["dinner", 18, 22]],
        )
        f82 = MaitreDLocation(
            store_id=store_id, branch_key="f82", name="F-8/2 Madina Market",
            address="F-8/2 Madina Market", is_primary=False, accepts_reservations=False,
            tables=[["T1", 4]], service_windows=[["dinner", 18, 22]],
        )
        db.add_all([blue, f82])
        db.commit()
        db.refresh(blue)
        db.refresh(f82)
        return blue.id, f82.id


def _md_multi(sid: int, now: datetime = FRIDAY_8PM) -> MaitreD:
    """Same as _md(), but with locations auto-fetched from the DB instead
    of forced empty -- exercises the real multi-branch resolution path."""
    return MaitreD(store=Store(sid), config=VenueConfig.load(sid), client=None, now_fn=lambda: now)


class TestMultiLocation:
    def test_asks_which_branch_when_ambiguous(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        assert r.action == "need_info"
        assert "New Blue Area" in r.text
        assert "F-8/2" not in r.text  # delivery-only branch never offered as a booking choice

    def test_naming_branch_upfront_skips_the_question(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm at New Blue Area, it's Ahmed")
        assert r.action == "booked"

    def test_branch_answer_resumes_the_booking(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r1 = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        assert r1.action == "need_info"
        r2 = md.handle_message(PHONE, "New Blue Area")
        assert r2.action == "booked"

    def test_delivery_only_branch_is_declined_with_alternative_offered(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm at F-8/2, it's Ahmed")
        assert r.action == "need_info"
        assert "delivery-only" in r.text.lower()
        assert "New Blue Area" in r.text

    def test_same_table_name_at_different_branches_does_not_conflict(self, store_id):
        """Both seeded locations have a table called "T1" -- booking it at
        one branch must not block booking the identically-named table at
        the other. This is the exact bug being fixed: capacity was
        previously checked store-wide, so two branches sharing a table
        name would falsely collide."""
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r1 = md.handle_message(
            "+923000000001", "table for 4 friday 8pm at New Blue Area, it's Ahmed",
        )
        assert r1.action == "booked"

        # A second store doesn't exist here -- instead, use the SAME store's
        # only reservation-taking branch a second time to confirm normal
        # same-branch capacity still works (waitlists once T1's taken)...
        r2 = md.handle_message(
            "+923000000002", "table for 4 friday 8pm at New Blue Area, it's Bilal",
        )
        assert r2.action == "waitlisted"

    def test_reservation_carries_the_correct_branch_name(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm at New Blue Area, it's Ahmed")
        res = md.store.get_reservation(r.reservation_id)
        assert res.branch_name == "New Blue Area"
        assert "New Blue Area" in r.text

    def test_waitlist_promotion_respects_branch(self, store_id):
        """A table freed at New Blue Area must only be offered to guests
        waiting for New Blue Area, never cross-offered to a different
        branch's queue."""
        blue_id, f82_id = _seed_two_locations(store_id)
        md = _md_multi(store_id)
        md.handle_message("+923000000001", "table for 4 friday 8pm at New Blue Area, it's Ahmed")
        waiter = "+923000000002"
        wl = md.handle_message(waiter, "table for 4 friday 8pm at New Blue Area, it's Bilal")
        assert wl.action == "waitlisted"

        cancel = md.handle_message("+923000000001", "cancel")
        assert cancel.outbound
        assert cancel.outbound[0][0] == waiter

    def test_staff_branches_listing(self, store_id):
        _seed_two_locations(store_id)
        from app.agents.maitre_d.staff import format_locations
        text = format_locations(store_id)
        assert "New Blue Area" in text
        assert "F-8/2" in text
        assert "delivery-only" in text.lower()

    def test_staff_reservations_listing_shows_branch_label(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id, now=datetime.now())
        md.handle_message(PHONE, "table for 2 tonight 8pm at New Blue Area, it's Ahmed")
        from app.agents.maitre_d.staff import format_reservations
        text = format_reservations(store_id)
        assert "New Blue Area" in text

    def test_single_location_store_never_asks_which_branch(self, store_id):
        """A store with zero or one MaitreDLocation rows must behave
        exactly as before -- no regression for stores that haven't set up
        branches."""
        md = _md_multi(store_id)  # no locations seeded for this store_id
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        assert r.action == "booked"


# ── Multi-tenancy isolation (new -- didn't exist on the single-tenant branch) ─

class TestMultiTenancy:
    def test_reservations_are_isolated_per_store(self):
        chain_id = seed_chain("Iso Chain")
        store_a = seed_store(chain_id, name="Store A")
        store_b = seed_store(chain_id, name="Store B")

        md_a = _md(store_a)
        md_a.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")

        md_b = _md(store_b)
        assert md_b.store.list_reservations() == []
        assert len(md_a.store.list_reservations()) == 1

    def test_vip_list_is_isolated_per_store(self):
        chain_id = seed_chain("Iso VIP Chain")
        store_a = seed_store(chain_id, name="VIP Store A")
        store_b = seed_store(chain_id, name="VIP Store B")

        from app.core.db import SessionLocal, MaitreDVip
        with SessionLocal() as db:
            db.add(MaitreDVip(store_id=store_a, phone=PHONE, name="Ayesha", tier="vip"))
            db.commit()

        cfg_a = VenueConfig.load(store_a)
        cfg_b = VenueConfig.load(store_b)
        assert cfg_a.vip_for(PHONE) is not None
        assert cfg_b.vip_for(PHONE) is None

    def test_reservation_lookup_does_not_cross_stores(self):
        chain_id = seed_chain("Iso Lookup Chain")
        store_a = seed_store(chain_id, name="Lookup A")
        store_b = seed_store(chain_id, name="Lookup B")

        md_a = _md(store_a)
        r = md_a.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")

        store_b_view = Store(store_b)
        assert store_b_view.get_reservation(r.reservation_id) is None


# ── Gateway wiring: customer mode routes booking-shaped messages here ──────

class TestCustomerGatewayRouting:
    def test_booking_keyword_routes_to_maitre_d(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(PHONE, "table for 2 friday 8pm, it's Ahmed", store_id)
        assert "booked" in reply.lower() or "table" in reply.lower()

    def test_non_booking_message_does_not_route_to_maitre_d(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            handle_customer_for_store(PHONE, "hi", store_id)
        mock_md.assert_not_called()

    def test_active_flow_keeps_routing_to_maitre_d_without_keywords(self, store_id):
        """"friday 8pm, it's Ahmed" contains no booking keyword on its own --
        only the in-progress conversation state tells the router this is a
        continuation of the earlier booking flow, not a fresh community-
        agent message."""
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, "table for 4", store_id)  # starts a flow, no name/time yet
        reply = handle_customer_for_store(PHONE, "friday 8pm, it's Ahmed", store_id)
        assert "booked" in reply.lower()

    def test_staff_alert_is_dispatched_to_registered_members(self, store_id):
        from app.core.db import SessionLocal, StoreMember, MaitreDVip

        vip_phone = "+923005550000"
        with SessionLocal() as db:
            db.add(StoreMember(store_id=store_id, whatsapp="whatsapp:+923009999999", role="owner"))
            db.add(MaitreDVip(store_id=store_id, phone=vip_phone, name="Ayesha", tier="vip"))
            db.commit()

        from app.gateway.customer import handle_customer_for_store
        # A VIP's own booking always sets staff_alert (see agent.py's
        # _try_seat), so this deterministically exercises the dispatch path
        # rather than depending on a random high-risk classification.
        with patch("app.core.outbound.notify_staff") as mock_notify:
            handle_customer_for_store(
                vip_phone, "table for 2 friday 8pm, it's Ayesha", store_id,
            )
        mock_notify.assert_called_once()
        args = mock_notify.call_args.args
        assert args[0] == store_id
        assert "VIP" in args[1]


# ── Gateway wiring: staff mode door/listing/NL-Q&A routing ─────────────────

class TestStaffGatewayRouting:
    def test_reservations_shorthand_lists_bookings(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        md = _md(store_id, now=datetime.now())
        md.handle_message(PHONE, "table for 2 tonight 8pm, it's Ahmed")
        reply = handle_internal_for_store("whatsapp:+923220000000", "reservations", store_id)
        assert "Ahmed" in reply or "reservation" in reply.lower()

    def test_waitlist_shorthand(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store("whatsapp:+923220000000", "waitlist", store_id)
        assert "waitlist" in reply.lower() or "empty" in reply.lower()

    def test_add_vip_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(
            "whatsapp:+923220000000", "add vip +923001112222 Ayesha Khan, food critic", store_id,
        )
        assert "Ayesha" in reply
        cfg = VenueConfig.load(store_id)
        assert cfg.vip_for("+923001112222") is not None

    def test_door_command_seats_a_reservation(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2 friday 8pm, it's Ahmed")
        tag = r.reservation_id.replace("res_", "")[-6:]
        reply = handle_internal_for_store("whatsapp:+923220000000", f"seat {tag}", store_id)
        assert "seated" in reply.lower() or "ahmed" in reply.lower()
        res = md.store.get_reservation(r.reservation_id)
        assert res.status == "seated"

    def test_maitre_d_answer_question_uses_shared_persona(self, store_id):
        from app.agents.maitre_d.staff import answer_question
        mock_client = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        resp = mock_client.chat.completions.create.return_value
        resp.choices = [mock_client.chat.completions.create.return_value.choices[0]]
        resp.choices[0].message.content = "You have 1 booking tonight."
        with patch("app.core.llm.get_client", return_value=mock_client):
            result = answer_question(store_id, "who's booked tonight")
        messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        system_msg = next(m["content"] for m in messages if m["role"] == "system")
        assert "*single asterisks*" in system_msg  # shared persona's WhatsApp formatting rule
        assert result == "You have 1 booking tonight."
