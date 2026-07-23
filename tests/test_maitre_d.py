"""Maitre D — the live walk-in queue and the door.

The guest-facing flow is deliberately simple: say "book", get asked for
whatever isn't already known (branch if multi-location, name if not saved,
party size), then get a permanent booking number back. Staff manage the
live line with admit/remove/add-at-position. There is no date/time
table-reservation model, no deposits, no no-show scoring any more -- see
app/agents/maitre_d/__init__.py's module docstring.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests.conftest import seed_chain, seed_store

from app.agents.maitre_d.agent import MaitreD, MaitreDReply
from app.agents.maitre_d.config import VenueConfig, VipProfile
from app.agents.maitre_d.store import Store
from app.agents.maitre_d.nlu import parse_message


@pytest.fixture
def store_id():
    chain_id = seed_chain("MD Chain")
    return seed_store(chain_id, name="MD Test Cafe", location="F-7, Islamabad")


FRIDAY_8PM = datetime(2026, 7, 17, 20, 0)  # a Friday


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

    def test_intents(self):
        assert parse_message("cancel my booking").intent == "cancel"
        assert parse_message("hi there").intent == "greeting"
        assert parse_message("table for 4").intent == "book"

    def test_bare_party_size_implies_booking(self):
        assert parse_message("4 people please").intent == "book"

    def test_name_and_requests_extracted(self):
        parsed = parse_message("table for 2, it's Ayesha, window seat please")
        assert parsed.name == "Ayesha"
        assert "window" in parsed.special_requests


# ── Joining the queue ────────────────────────────────────────────────────────

class TestQueueJoin:
    def test_book_joins_queue_and_returns_a_number(self, store_id):
        md = _md(store_id)
        reply = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert reply.action == "queued"
        assert reply.queue_number == 1
        assert "Hi Ahmed" in reply.text
        assert "booking number" in reply.text
        assert "1" in reply.text

    def test_slot_filling_across_messages(self, store_id):
        md = _md(store_id)
        r1 = md.handle_message(PHONE, "book")
        assert r1.action == "need_info"
        r2 = md.handle_message(PHONE, "party of 4")
        assert r2.action == "need_info"  # still needs a name
        r3 = md.handle_message(PHONE, "it's Ahmed")
        assert r3.action == "queued"

    def test_saved_name_is_not_asked_for_again(self, store_id):
        md = _md(store_id)
        md.handle_message(PHONE, "table for 2, it's Ahmed")  # saves the guest's name

        md2 = _md(store_id)
        r = md2.handle_message(PHONE, "table for 3")
        assert r.action == "queued"  # name filled in silently from the saved guest record
        assert "Ahmed" in r.text

    def test_profile_name_used_when_nothing_saved(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2", profile_name="Bilal")
        assert r.action == "queued"
        assert "Bilal" in r.text

    def test_queue_numbers_are_sequential(self, store_id):
        md = _md(store_id)
        r1 = md.handle_message("+923000000001", "table for 2, it's Aman")
        r2 = md.handle_message("+923000000002", "table for 2, it's Bilal")
        r3 = md.handle_message("+923000000003", "table for 2, it's Cyrus")
        assert [r1.queue_number, r2.queue_number, r3.queue_number] == [1, 2, 3]


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
        r = md.handle_message(PHONE, "table for 2")
        assert r.is_vip
        assert r.action == "queued"
        assert "VIP" in r.staff_alert

    def test_vip_queues_like_everyone_else(self, store_id):
        """Pure FIFO -- a VIP takes a normal spot at the back, staff can
        move them manually with admit/remove/add if they want to."""
        md = _md(store_id, vips={PHONE: VipProfile(name="Ayesha", tier="vip")})
        md.handle_message("+923000000001", "table for 2, it's Regular")
        r = md.handle_message(PHONE, "table for 2")
        assert r.queue_number == 2
        assert r.position == 2


# ── Leaving the queue ────────────────────────────────────────────────────────

class TestLeaveQueue:
    def test_cancel_removes_from_queue(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2, it's Ahmed")
        cancel = md.handle_message(PHONE, "cancel")
        assert cancel.action == "cancelled"
        assert cancel.queue_number == r.queue_number
        assert md.store.list_queue(status="waiting") == []

    def test_cancel_with_no_booking_is_graceful(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "cancel")
        assert r.action == "noop"

    def test_queue_moves_up_after_a_cancel(self, store_id):
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        md.handle_message("+923000000001", "cancel")
        remaining = md.store.list_queue(status="waiting")
        assert len(remaining) == 1
        assert remaining[0].position == 1

    def test_cancel_notifies_the_guest_behind_of_their_new_position(self, store_id):
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        cancel = md.handle_message("+923000000001", "cancel")
        assert cancel.outbound == [("+923000000002", "You're now #1 in line at MD Test Cafe.")]


# ── Staff: admit / remove / insert ──────────────────────────────────────────

class TestStaffQueueOps:
    def test_admit_pops_the_front_and_shifts_the_rest(self, store_id):
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        store = Store(store_id)
        admitted, moved_up = store.admit_next(None)
        assert admitted.name == "Aman"
        assert admitted.status == "admitted"
        assert [e.name for e in moved_up] == ["Bilal"]
        assert moved_up[0].position == 1
        remaining = store.list_queue(status="waiting")
        assert len(remaining) == 1
        assert remaining[0].name == "Bilal"
        assert remaining[0].position == 1

    def test_admit_on_empty_queue_returns_none(self, store_id):
        assert Store(store_id).admit_next(None) == (None, [])

    def test_remove_at_position_shifts_the_rest(self, store_id):
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        md.handle_message("+923000000003", "table for 2, it's Cyrus")
        store = Store(store_id)
        removed, moved_up = store.remove_at_position(None, 2)
        assert removed.name == "Bilal"
        assert [e.name for e in moved_up] == ["Cyrus"]
        remaining = store.list_queue(status="waiting")
        assert [e.name for e in remaining] == ["Aman", "Cyrus"]
        assert [e.position for e in remaining] == [1, 2]

    def test_insert_at_position_shifts_the_rest_back(self, store_id):
        from app.agents.maitre_d.models import QueueEntry
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        store = Store(store_id)
        entry = QueueEntry(phone="+923000000099", name="Inserted", party_size=2)
        _new, pushed_back = store.insert_at_position(entry, position=1, day_start=FRIDAY_8PM.replace(hour=0, minute=0))
        assert [e.name for e in pushed_back] == ["Aman", "Bilal"]
        ordered = store.list_queue(status="waiting")
        assert [e.name for e in ordered] == ["Inserted", "Aman", "Bilal"]
        assert [e.position for e in ordered] == [1, 2, 3]

    def test_insert_position_clamps_past_the_end(self, store_id):
        from app.agents.maitre_d.models import QueueEntry
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        store = Store(store_id)
        entry = QueueEntry(phone="+923000000099", name="Inserted", party_size=2)
        saved, pushed_back = store.insert_at_position(entry, position=99, day_start=FRIDAY_8PM.replace(hour=0, minute=0))
        assert saved.position == 2
        assert pushed_back == []


# ── Notifications on every queue mutation ───────────────────────────────────

class TestQueueNotifications:
    def test_admit_notifies_the_admitted_guest_and_the_rest(self, store_id):
        from app.agents.maitre_d.staff import admit_next_in_queue
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        with patch("app.core.outbound.send_from_store") as mock_send:
            admit_next_in_queue(store_id)
        calls = {c.args[1]: c.args[2] for c in mock_send.call_args_list}
        assert "seated" in calls["+923000000001"].lower()
        assert calls["+923000000002"] == "You're now #1 in line at MD Test Cafe."

    def test_remove_position_does_not_notify_the_removed_guest(self, store_id):
        from app.agents.maitre_d.staff import remove_queue_position
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        with patch("app.core.outbound.send_from_store") as mock_send:
            remove_queue_position(store_id, "1")
        calls = {c.args[1]: c.args[2] for c in mock_send.call_args_list}
        assert "+923000000001" not in calls  # removed guest stays silent
        assert calls["+923000000002"] == "You're now #1 in line at MD Test Cafe."

    def test_insert_notifies_everyone_pushed_back(self, store_id):
        from app.agents.maitre_d.staff import insert_queue_position
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        with patch("app.core.outbound.send_from_store") as mock_send:
            insert_queue_position(store_id, "1 +923009998888 Walked In, party 2")
        calls = {c.args[1]: c.args[2] for c in mock_send.call_args_list}
        assert calls["+923000000001"] == "You're now #2 in line at MD Test Cafe."
        assert "+923009998888" not in calls  # the newly-inserted guest isn't separately notified here


# ── "Already seated" guard (a table's own QR code can't stop someone
# seated there from typing "book" -- this is the server-side guard) ────────

class TestSeatedGuard:
    def test_book_declined_while_recently_seated(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        Store(store_id).admit_next(None, now=FRIDAY_8PM)  # staff seats Ahmed

        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=30))
        later.store = md.store
        r = later.handle_message(PHONE, "book")
        assert r.action == "already_seated"
        assert "already seated" in r.text.lower()

    def test_seated_guest_does_not_get_a_new_queue_entry(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        Store(store_id).admit_next(None, now=FRIDAY_8PM)

        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=30))
        later.store = md.store
        later.handle_message(PHONE, "book")
        assert later.store.list_queue(status="waiting") == []

    def test_book_allowed_again_after_the_grace_window_passes(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        Store(store_id).admit_next(None, now=FRIDAY_8PM)

        much_later = _md(store_id, now=FRIDAY_8PM + timedelta(hours=3))
        much_later.store = md.store
        r = much_later.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r.action == "queued"

    def test_waiting_but_not_yet_admitted_guest_is_unaffected(self, store_id):
        """Still in line, never seated -- the guard only fires on an
        actual admission, not merely having a booking already."""
        md = _md(store_id)
        r = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r.action == "queued"


# ── Conversation TTL ─────────────────────────────────────────────────────────

class TestConversationTTL:
    def test_stale_slot_fill_is_forgotten(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "party of 4")  # starts a flow, missing name

        later = _md(store_id, now=FRIDAY_8PM + timedelta(hours=4))  # past 180min TTL
        later.store = md.store
        state = later._conversation(PHONE)
        assert state == {}


# ── Multi-location branches (Anatummy has three; a queue entry must be tied
# to one specific branch, not conflated across physically different
# addresses) ─────────────────────────────────────────────────────────────

def _seed_two_locations(store_id: int) -> tuple[int, int]:
    """New Blue Area (primary, dine-in) + F-8/2 (delivery-only) -- mirrors
    Anatummy's real setup. Returns (blue_area_id, f82_id)."""
    from app.core.db import SessionLocal, MaitreDLocation

    with SessionLocal() as db:
        blue = MaitreDLocation(
            store_id=store_id, branch_key="new_blue_area", name="New Blue Area",
            address="Skyline Tower, G-9/2", is_primary=True, accepts_reservations=True,
        )
        f82 = MaitreDLocation(
            store_id=store_id, branch_key="f82", name="F-8/2 Madina Market",
            address="F-8/2 Madina Market", is_primary=False, accepts_reservations=False,
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
        r = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r.action == "need_info"
        assert "New Blue Area" in r.text
        assert "F-8/2" not in r.text  # delivery-only branch never offered as a booking choice

    def test_naming_branch_upfront_skips_the_question(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "table for 2 at New Blue Area, it's Ahmed")
        assert r.action == "queued"

    def test_branch_answer_resumes_the_booking(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r1 = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r1.action == "need_info"
        r2 = md.handle_message(PHONE, "New Blue Area")
        assert r2.action == "queued"

    def test_branch_question_is_numbered_and_a_number_reply_resolves_it(self, store_id):
        """Two branches that BOTH take walk-ins -- a real numbered choice,
        not the single-option case _seed_two_locations gives."""
        from app.core.db import SessionLocal, MaitreDLocation
        with SessionLocal() as db:
            db.add_all([
                MaitreDLocation(store_id=store_id, branch_key="blue", name="Blue Area", is_primary=True, accepts_reservations=True),
                MaitreDLocation(store_id=store_id, branch_key="bahria", name="Bahria Town", accepts_reservations=True),
            ])
            db.commit()
        md = _md_multi(store_id)
        r1 = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r1.action == "need_info"
        assert "1. Blue Area" in r1.text
        assert "2. Bahria Town" in r1.text
        r2 = md.handle_message(PHONE, "2")
        assert r2.action == "queued"
        assert "Bahria Town" in r2.text

    def test_delivery_only_branch_is_declined_with_alternative_offered(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "table for 2 at F-8/2, it's Ahmed")
        assert r.action == "need_info"
        assert "delivery-only" in r.text.lower()
        assert "New Blue Area" in r.text

    def test_queue_numbers_scoped_per_branch(self, store_id):
        """Both branches start their own numbering at 1 -- a booking
        number is only unique within its own branch's line."""
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r1 = md.handle_message(
            "+923000000001", "table for 2 at New Blue Area, it's Ahmed",
        )
        assert r1.queue_number == 1
        assert "New Blue Area" in r1.text

    def test_staff_branches_listing(self, store_id):
        _seed_two_locations(store_id)
        from app.agents.maitre_d.staff import format_locations
        text = format_locations(store_id)
        assert "New Blue Area" in text
        assert "F-8/2" in text
        assert "delivery-only" in text.lower()

    def test_staff_queue_listing_shows_branch_label(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id, now=datetime.now())
        md.handle_message(PHONE, "table for 2 at New Blue Area, it's Ahmed")
        from app.agents.maitre_d.staff import format_queue
        text = format_queue(store_id)
        assert "New Blue Area" in text

    def test_single_location_store_never_asks_which_branch(self, store_id):
        """A store with zero or one MaitreDLocation rows must behave
        exactly as before -- no regression for stores that haven't set up
        branches."""
        md = _md_multi(store_id)  # no locations seeded for this store_id
        r = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r.action == "queued"

    def test_staff_admit_requires_a_branch_for_multi_location_stores(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        md.handle_message(PHONE, "table for 2 at New Blue Area, it's Ahmed")
        from app.agents.maitre_d.staff import admit_next_in_queue
        ambiguous = admit_next_in_queue(store_id, "")
        assert "which branch" in ambiguous.lower()
        resolved = admit_next_in_queue(store_id, "at New Blue Area")
        assert "Admitted" in resolved
        assert "Ahmed" in resolved


# ── Multi-tenancy isolation ──────────────────────────────────────────────────

class TestMultiTenancy:
    def test_queue_is_isolated_per_store(self):
        chain_id = seed_chain("Iso Chain")
        store_a = seed_store(chain_id, name="Store A")
        store_b = seed_store(chain_id, name="Store B")

        md_a = _md(store_a)
        md_a.handle_message(PHONE, "table for 2, it's Ahmed")

        md_b = _md(store_b)
        assert md_b.store.list_queue(status="waiting") == []
        assert len(md_a.store.list_queue(status="waiting")) == 1

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


# ── Gateway wiring: customer mode routes booking-shaped messages here ──────

class TestCustomerGatewayRouting:
    def test_qr_trigger_phrase_starts_the_queue_flow(self, store_id):
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        r1 = handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)
        assert r1 != "Something went wrong, please try again."
        assert "queue" in r1.lower() or "name" in r1.lower() or "many" in r1.lower()
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_decorated_trigger_phrase_still_matches(self, store_id):
        """The printed QR text can be decorated ("\U0001f3ab Join the Queue!")
        without breaking the match -- see customer.py's alnum-normalised
        comparison."""
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, "\U0001f3ab Join the Queue!", store_id)
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_bare_book_keyword_no_longer_routes_to_maitre_d(self, store_id):
        """The old free-text trigger ("book"/"table for 2") must NOT start
        a queue join any more -- otherwise someone already seated at a
        table (or just texting from home) could type their way into the
        queue. Only the exact QR trigger phrase, or an already-active
        flow, may."""
        from app.gateway.customer import handle_customer_for_store
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            handle_customer_for_store(PHONE, "table for 2, it's Ahmed", store_id)
        mock_md.assert_not_called()

    def test_non_booking_message_does_not_route_to_maitre_d(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            handle_customer_for_store(PHONE, "hi", store_id)
        mock_md.assert_not_called()

    def test_active_flow_keeps_routing_to_maitre_d_without_the_trigger(self, store_id):
        """"it's Ahmed" is not the trigger phrase on its own -- only the
        in-progress conversation state tells the router this is a
        continuation of the earlier booking flow, not a fresh community-
        agent message."""
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)  # starts a flow, no name/party yet
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_cancel_always_works_regardless_of_the_trigger_phrase(self, store_id):
        """"cancel" only ever removes an existing entry, so it's exempt
        from the trigger-phrase restriction (see _is_cancel_message)."""
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)
        handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        reply = handle_customer_for_store(PHONE, "cancel please", store_id)
        assert "removed" in reply.lower()

    def test_disabled_booking_silently_falls_through(self, store_id):
        """A disabled store's guests get routed to the normal community
        agent with NO "booking is off" message -- they should never even
        learn the queue exists if staff have switched it off."""
        from app.agents.maitre_d.config import set_booking_enabled
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        set_booking_enabled(store_id, False)
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            reply = handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)
        mock_md.assert_not_called()
        assert "off" not in reply.lower() and "disabled" not in reply.lower()

    def test_re_enabled_booking_works_again(self, store_id):
        from app.agents.maitre_d.config import set_booking_enabled
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        set_booking_enabled(store_id, False)
        set_booking_enabled(store_id, True)
        handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_staff_alert_is_dispatched_to_registered_members(self, store_id):
        from app.core.db import SessionLocal, StoreMember, MaitreDVip

        vip_phone = "+923005550000"
        with SessionLocal() as db:
            db.add(StoreMember(store_id=store_id, whatsapp="whatsapp:+923009999999", role="owner"))
            db.add(MaitreDVip(store_id=store_id, phone=vip_phone, name="Ayesha", tier="vip"))
            db.commit()

        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        # A VIP's own booking always sets staff_alert (see agent.py's
        # _join_queue), so this deterministically exercises the dispatch
        # path rather than depending on any randomised classification.
        handle_customer_for_store(vip_phone, BOOKING_TRIGGER_PHRASE, store_id)
        with patch("app.core.outbound.notify_staff") as mock_notify:
            handle_customer_for_store(vip_phone, "it's Ayesha, party of 2", store_id)
        mock_notify.assert_called_once()
        args = mock_notify.call_args.args
        assert args[0] == store_id
        assert "VIP" in args[1]


# ── Gateway wiring: staff mode queue/listing/NL-Q&A routing ────────────────

class TestStaffGatewayRouting:
    def test_queue_shorthand_lists_the_line(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        md = _md(store_id, now=datetime.now())
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        reply = handle_internal_for_store("whatsapp:+923220000000", "queue", store_id)
        assert "Ahmed" in reply

    def test_queue_shorthand_reports_empty(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store("whatsapp:+923220000000", "queue", store_id)
        assert "empty" in reply.lower()

    def test_disable_and_enable_booking_commands(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        from app.agents.maitre_d.config import is_booking_enabled

        assert is_booking_enabled(store_id) is True  # never toggled -> on by default
        off_reply = handle_internal_for_store("whatsapp:+923220000000", "disable booking", store_id)
        assert "off" in off_reply.lower()
        assert is_booking_enabled(store_id) is False

        on_reply = handle_internal_for_store("whatsapp:+923220000000", "enable booking", store_id)
        assert "on" in on_reply.lower()
        assert is_booking_enabled(store_id) is True

    def test_queue_listing_flags_when_booking_is_off(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        handle_internal_for_store("whatsapp:+923220000000", "disable booking", store_id)
        reply = handle_internal_for_store("whatsapp:+923220000000", "queue", store_id)
        assert "off" in reply.lower()

    def test_add_vip_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(
            "whatsapp:+923220000000", "add vip +923001112222 Ayesha Khan, food critic", store_id,
        )
        assert "Ayesha" in reply
        cfg = VenueConfig.load(store_id)
        assert cfg.vip_for("+923001112222") is not None

    def test_admit_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        md = _md(store_id)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        reply = handle_internal_for_store("whatsapp:+923220000000", "admit", store_id)
        assert "Admitted" in reply
        assert "Ahmed" in reply
        assert md.store.list_queue(status="waiting") == []

    def test_remove_position_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        md.handle_message("+923000000002", "table for 2, it's Bilal")
        reply = handle_internal_for_store("whatsapp:+923220000000", "remove 1", store_id)
        assert "Removed" in reply
        remaining = md.store.list_queue(status="waiting")
        assert [e.name for e in remaining] == ["Bilal"]

    def test_add_position_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        reply = handle_internal_for_store(
            "whatsapp:+923220000000", "add 1 +923009998888 Walked In, party 3", store_id,
        )
        assert "Added" in reply
        ordered = md.store.list_queue(status="waiting")
        assert [e.name for e in ordered] == ["Walked In", "Aman"]
        assert ordered[0].party_size == 3

    def test_maitre_d_answer_question_uses_shared_persona(self, store_id):
        from app.agents.maitre_d.staff import answer_question
        mock_client = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        resp = mock_client.chat.completions.create.return_value
        resp.choices = [mock_client.chat.completions.create.return_value.choices[0]]
        resp.choices[0].message.content = "You have 1 person waiting."
        with patch("app.core.llm.get_client", return_value=mock_client):
            result = answer_question(store_id, "who's in the queue")
        messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        system_msg = next(m["content"] for m in messages if m["role"] == "system")
        assert "*single asterisks*" in system_msg  # shared persona's WhatsApp formatting rule
        assert result == "You have 1 person waiting."
