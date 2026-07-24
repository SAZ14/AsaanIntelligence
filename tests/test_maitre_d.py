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
from unittest.mock import patch, MagicMock

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
        md.handle_message(PHONE, "cancel")  # that visit ends before the next one starts

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

    def test_branch_code_in_the_message_itself_skips_the_question(self, store_id):
        """A branch-specific QR's prefilled text ("Join the Queue -
        new_blue_area") carries the branch_key right in the message --
        _book_flow's existing free-text location matching (match_location)
        finds it on the very first turn, so the guest is asked for name/
        party size directly, never "which branch"."""
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "Join the Queue - new_blue_area")
        assert r.action == "need_info"
        assert "which branch" not in r.text.lower()
        r2 = md.handle_message(PHONE, "party of 2, it's Ahmed")
        assert r2.action == "queued"
        assert "New Blue Area" in r2.text

    def test_branch_code_pointing_at_delivery_only_branch_is_declined(self, store_id):
        """Same decline-with-alternative behavior as naming the branch in
        plain text (test_delivery_only_branch_is_declined_with_alternative_
        offered) -- a QR code is just another way of naming the branch."""
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        r = md.handle_message(PHONE, "Join the Queue - f82")
        assert r.action == "need_info"
        assert "delivery-only" in r.text.lower()
        assert "New Blue Area" in r.text


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

def _entrance_text(store_id: int, suffix: str = "") -> str:
    """A fresh, valid "Join the Queue[ - branch] #CODE" message -- what a
    REAL scan of the entrance QR relinker (main.py's entrance_qr_relink)
    hands back. Any test that drives the flow through the real gateway
    entry point (handle_customer_for_store, as opposed to calling
    MaitreD.handle_message directly) needs this: a fresh trigger now also
    requires a currently-valid one-time code, see customer.py's
    _redeem_entrance_code."""
    from app.agents.maitre_d.store import Store
    from app.gateway.customer import BOOKING_TRIGGER_PHRASE
    code = Store(store_id).generate_entrance_code(None)
    return f"{BOOKING_TRIGGER_PHRASE}{suffix} #{code}"


class TestCustomerGatewayRouting:
    def test_qr_trigger_phrase_starts_the_queue_flow(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        r1 = handle_customer_for_store(PHONE, _entrance_text(store_id), store_id)
        assert r1 != "Something went wrong, please try again."
        assert "queue" in r1.lower() or "name" in r1.lower() or "many" in r1.lower()
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_decorated_trigger_phrase_still_matches(self, store_id):
        """The printed QR text can be decorated ("\U0001f3ab Join the Queue!")
        without breaking the match -- see customer.py's alnum-normalised
        comparison."""
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, f"\U0001f3ab {_entrance_text(store_id, '!')}", store_id)
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_qr_trigger_with_branch_code_skips_the_branch_question(self, store_id):
        """End-to-end through the real gateway entry point (not MaitreD
        directly): scanning a branch-specific QR ("Join the Queue -
        new_blue_area") joins that branch's queue without ever asking
        which branch."""
        _seed_two_locations(store_id)
        from app.gateway.customer import handle_customer_for_store
        r1 = handle_customer_for_store(PHONE, _entrance_text(store_id, " - new_blue_area"), store_id)
        assert "which branch" not in r1.lower()
        r2 = handle_customer_for_store(PHONE, "party of 2, it's Ahmed", store_id)
        assert "booking number" in r2.lower()
        assert "new blue area" in r2.lower()

    def test_trigger_prefix_match_rejects_the_phrase_mid_sentence(self, store_id):
        """The base phrase must still be typed at the START of the message
        -- "please join the queue for me" (ordinary chat that happens to
        contain the words) must NOT trigger a fresh booking, same
        protection the old exact-match version had against accidental
        typing."""
        from app.gateway.customer import handle_customer_for_store
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            handle_customer_for_store(PHONE, "please join the queue for me sometime", store_id)
        mock_md.assert_not_called()

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
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, _entrance_text(store_id), store_id)  # starts a flow, no name/party yet
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_cancel_always_works_regardless_of_the_trigger_phrase(self, store_id):
        """"cancel" only ever removes an existing entry, so it's exempt
        from the trigger-phrase restriction (see _is_cancel_message)."""
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, _entrance_text(store_id), store_id)
        handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        reply = handle_customer_for_store(PHONE, "cancel please", store_id)
        assert "removed" in reply.lower()

    def test_disabled_booking_silently_falls_through(self, store_id):
        """A disabled store's guests get routed to the normal community
        agent with NO "booking is off" message -- they should never even
        learn the queue exists if staff have switched it off. No entrance
        code needed here: booking-disabled is checked BEFORE a code is
        ever looked at, so even a genuine fresh trigger with a valid code
        would still fall through the same way."""
        from app.agents.maitre_d.config import set_booking_enabled
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        set_booking_enabled(store_id, False)
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            reply = handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)
        mock_md.assert_not_called()
        assert "off" not in reply.lower() and "disabled" not in reply.lower()

    def test_re_enabled_booking_works_again(self, store_id):
        from app.agents.maitre_d.config import set_booking_enabled
        from app.gateway.customer import handle_customer_for_store
        set_booking_enabled(store_id, False)
        set_booking_enabled(store_id, True)
        handle_customer_for_store(PHONE, _entrance_text(store_id), store_id)
        reply = handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        assert "booking number" in reply.lower()

    def test_staff_alert_is_dispatched_to_registered_members(self, store_id):
        from app.core.db import SessionLocal, StoreMember, MaitreDVip

        vip_phone = "+923005550000"
        with SessionLocal() as db:
            db.add(StoreMember(store_id=store_id, whatsapp="whatsapp:+923009999999", role="owner"))
            db.add(MaitreDVip(store_id=store_id, phone=vip_phone, name="Ayesha", tier="vip"))
            db.commit()

        from app.gateway.customer import handle_customer_for_store
        # A VIP's own booking always sets staff_alert (see agent.py's
        # _join_queue), so this deterministically exercises the dispatch
        # path rather than depending on any randomised classification.
        handle_customer_for_store(vip_phone, _entrance_text(store_id), store_id)
        with patch("app.core.outbound.notify_staff") as mock_notify:
            handle_customer_for_store(vip_phone, "it's Ayesha, party of 2", store_id)
        mock_notify.assert_called_once()
        args = mock_notify.call_args.args
        assert args[0] == store_id
        assert "VIP" in args[1]


# ── Per-phone rate limit ─────────────────────────────────────────────────────

class TestCustomerRateLimit:
    def test_messages_within_the_limit_all_get_a_reply(self, store_id):
        from app.gateway.customer import handle_customer_for_store, _CUSTOMER_RATE_MAX
        with patch("app.agents.maitre_d.agent.get_maitre_d"):
            for _ in range(_CUSTOMER_RATE_MAX):
                reply = handle_customer_for_store(PHONE, "hi", store_id)
                assert reply != ""

    def test_exceeding_the_limit_silently_drops_further_messages(self, store_id):
        from app.gateway.customer import handle_customer_for_store, _CUSTOMER_RATE_MAX
        for _ in range(_CUSTOMER_RATE_MAX):
            handle_customer_for_store(PHONE, "hi", store_id)
        reply = handle_customer_for_store(PHONE, "hi", store_id)
        assert reply == ""

    def test_rate_limit_is_per_phone_not_global(self, store_id):
        from app.gateway.customer import handle_customer_for_store, _CUSTOMER_RATE_MAX
        for _ in range(_CUSTOMER_RATE_MAX):
            handle_customer_for_store(PHONE, "hi", store_id)
        assert handle_customer_for_store(PHONE, "hi", store_id) == ""
        # a different phone is unaffected by the first one's limit
        assert handle_customer_for_store("+923009998888", "hi", store_id) != ""

    def test_rate_limited_fresh_trigger_never_burns_the_entrance_code(self, store_id):
        """A code silently dropped by the rate limiter must stay valid --
        otherwise a burst of duplicate webhook deliveries (a known
        WhatsApp behavior) could burn a guest's own code before their
        real message is even processed."""
        from app.gateway.customer import handle_customer_for_store, _CUSTOMER_RATE_MAX
        from app.agents.maitre_d.store import Store
        for _ in range(_CUSTOMER_RATE_MAX):
            handle_customer_for_store(PHONE, "hi", store_id)
        text = _entrance_text(store_id)
        code = text.rsplit("#", 1)[1]
        dropped = handle_customer_for_store(PHONE, text, store_id)
        assert dropped == ""
        ok, _ = Store(store_id).redeem_entrance_code(code)
        assert ok is True


# ── Realistic webhook phone format ("whatsapp:+92...", not the bare "+92..."
# every other test in this file uses) -- regression coverage for a real bug
# confirmed live: handle_customer_for_store used to pass from_phone through
# to _has_active_booking_flow/_wants_to_modify_queue_entry RAW, but
# MaitreDConversation/MaitreDQueueEntry rows are always keyed by the
# NORMALIZED phone (MaitreD.handle_message normalizes internally). Every
# real webhook (Twilio/OpenWA/Meta) passes "whatsapp:+92...", so this
# mismatch silently broke EVERY multi-turn guest conversation in
# production -- a guest's second message (answering "how many people?")
# could never be recognised as a continuation and fell through to the
# community agent instead. The bare-PHONE constant every other test here
# uses happens to already be in normalized form, which is exactly why this
# was never caught until a real, realistically-formatted live test did.
WHATSAPP_PHONE = "whatsapp:+923001234567"


class TestRealisticWebhookPhoneFormat:
    def test_multi_turn_booking_completes_with_whatsapp_prefixed_phone(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        r1 = handle_customer_for_store(WHATSAPP_PHONE, _entrance_text(store_id), store_id)
        assert "which branch" not in r1.lower()
        r2 = handle_customer_for_store(WHATSAPP_PHONE, "party of 2, it's Ahmed", store_id)
        assert "booking number" in r2.lower(), (
            f"multi-turn continuation broke with a whatsapp:-prefixed phone: {r2!r}"
        )

    def test_cancel_works_with_whatsapp_prefixed_phone(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(WHATSAPP_PHONE, _entrance_text(store_id), store_id)
        handle_customer_for_store(WHATSAPP_PHONE, "party of 2, it's Ahmed", store_id)
        reply = handle_customer_for_store(WHATSAPP_PHONE, "cancel", store_id)
        assert "removed" in reply.lower()

    def test_modify_works_with_whatsapp_prefixed_phone(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(WHATSAPP_PHONE, _entrance_text(store_id), store_id)
        handle_customer_for_store(WHATSAPP_PHONE, "party of 2, it's Ahmed", store_id)
        with patch("app.gateway.customer._llm_confirms_modify_intent", return_value=True):
            reply = handle_customer_for_store(WHATSAPP_PHONE, "actually we're 5 now", store_id)
        assert "updated" in reply.lower() or "5" in reply

    def test_branch_coded_qr_trigger_with_whatsapp_prefixed_phone(self, store_id):
        _seed_two_locations(store_id)
        from app.gateway.customer import handle_customer_for_store
        r1 = handle_customer_for_store(WHATSAPP_PHONE, _entrance_text(store_id, " - new_blue_area"), store_id)
        assert "which branch" not in r1.lower()
        r2 = handle_customer_for_store(WHATSAPP_PHONE, "party of 2, it's Ahmed", store_id)
        assert "booking number" in r2.lower()
        assert "new blue area" in r2.lower()


# ── Entrance-code gate on the real gateway entry point ──────────────────────

class TestEntranceCodeGate:
    """handle_customer_for_store requires a currently-valid one-time code
    on a FRESH trigger (see customer.py's _redeem_entrance_code) -- this
    is what actually stops a remembered/screenshotted trigger message
    from working days or rooms away from the entrance, which the fixed
    trigger phrase alone never could."""

    def test_trigger_with_no_code_is_rejected(self, store_id):
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            reply = handle_customer_for_store(PHONE, BOOKING_TRIGGER_PHRASE, store_id)
        mock_md.assert_not_called()
        assert "expired" in reply.lower() or "used" in reply.lower()

    def test_trigger_with_an_already_used_code_is_rejected(self, store_id):
        from app.agents.maitre_d.store import Store
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        code = Store(store_id).generate_entrance_code(None)
        Store(store_id).redeem_entrance_code(code)  # burn it once, as if already used
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            reply = handle_customer_for_store(PHONE, f"{BOOKING_TRIGGER_PHRASE} #{code}", store_id)
        mock_md.assert_not_called()
        assert "expired" in reply.lower() or "used" in reply.lower()

    def test_trigger_with_an_expired_code_is_rejected(self, store_id):
        from datetime import datetime, timedelta
        from app.core.db import SessionLocal, MaitreDEntranceCode
        from app.agents.maitre_d.store import Store
        from app.gateway.customer import handle_customer_for_store, BOOKING_TRIGGER_PHRASE
        code = Store(store_id).generate_entrance_code(None)
        with SessionLocal() as db:
            row = db.query(MaitreDEntranceCode).filter(MaitreDEntranceCode.code == code).first()
            row.created_at = datetime.utcnow() - timedelta(minutes=999)
            db.commit()
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            reply = handle_customer_for_store(PHONE, f"{BOOKING_TRIGGER_PHRASE} #{code}", store_id)
        mock_md.assert_not_called()
        assert "expired" in reply.lower() or "used" in reply.lower()

    def test_the_same_valid_code_cannot_start_two_separate_queue_joins(self, store_id):
        """A screenshot of one successful "Join the Queue #CODE" message
        shared with a friend must not let them join too, even seconds
        later -- the code is burned on the FIRST successful redemption."""
        from app.gateway.customer import handle_customer_for_store
        text = _entrance_text(store_id)
        first = handle_customer_for_store("+923000000001", text, store_id)
        assert "expired" not in first.lower()
        second = handle_customer_for_store("+923000000002", text, store_id)
        assert "expired" in second.lower() or "used" in second.lower()

    def test_a_fresh_relink_produces_a_working_code(self, store_id):
        """End-to-end: the real /q/{store_id} relinker's own output is
        redeemable exactly once through the real gateway entry point."""
        from urllib.parse import unquote
        from fastapi.testclient import TestClient
        from app.gateway.main import app
        from app.gateway.customer import handle_customer_for_store
        from app.core.db import SessionLocal, StoreMetaNumber

        with SessionLocal() as db:
            db.add(StoreMetaNumber(
                store_id=store_id, phone_number_id="1", waba_id="1",
                access_token="t", display_number="+1 555-159-0482",
            ))
            db.commit()

        with TestClient(app) as client:
            r = client.get(f"/q/{store_id}", follow_redirects=False)
        location = unquote(r.headers["location"])
        text = "Join the Queue" + location.split("Join the Queue", 1)[1]

        reply = handle_customer_for_store(PHONE, text, store_id)
        assert "expired" not in reply.lower()


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


# ── Fix 1: _has_active_booking_flow must use the venue-local clock ─────────

class TestActiveFlowClockFix:
    def test_has_active_booking_flow_passes_venue_local_now(self, store_id):
        """Regression: this used to rely on get_conversation()'s own
        default (server time) instead of passing an explicit venue-local
        `now=` -- on a server whose clock reads behind the venue's, the
        staleness comparison was always negative, so an abandoned flow
        NEVER expired, letting that phone bypass the QR-trigger
        restriction forever. Checked mechanically (that `now` is passed
        at all) rather than via real-clock arithmetic, since the latter's
        outcome would depend on the test machine's own system timezone."""
        from app.gateway.customer import _has_active_booking_flow
        with patch("app.agents.maitre_d.store.Store.get_conversation") as mock_get:
            mock_get.return_value = {}
            _has_active_booking_flow(store_id, PHONE)
        assert mock_get.call_args.kwargs.get("now") is not None

    def test_stale_flow_actually_expires(self, store_id):
        from app.gateway.customer import _has_active_booking_flow
        from app.agents.maitre_d.store import Store
        from app.agents.maitre_d.config import VenueConfig

        store = Store(store_id)
        cfg = VenueConfig.load(store_id)
        stale = cfg.now() - timedelta(minutes=cfg.conversation_ttl_minutes + 10)
        store.set_conversation(PHONE, {"flow": "book", "slots": {}}, now=stale)
        assert _has_active_booking_flow(store_id, PHONE) is False


# ── Fix 2: resending the trigger must not create a duplicate entry ─────────

class TestDuplicateJoinGuard:
    def test_resending_the_trigger_does_not_create_a_duplicate(self, store_id):
        md = _md(store_id)
        r1 = md.handle_message(PHONE, "table for 2, it's Ahmed")
        r2 = md.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r2.action == "already_queued"
        assert r2.queue_number == r1.queue_number
        assert len(md.store.list_queue(status="waiting")) == 1

    def test_mid_flow_resend_also_does_not_duplicate(self, store_id):
        """Even if the second attempt is only PART-way through slot-filling
        (not yet a complete duplicate booking), it must still be caught
        once they already have a waiting entry from the first attempt."""
        md = _md(store_id)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        r = md.handle_message(PHONE, "book")
        assert r.action == "already_queued"
        assert len(md.store.list_queue(status="waiting")) == 1


# ── Fix 3: concurrent joins must never collide on number/position ──────────

class TestQueueConcurrencySafety:
    def test_lock_location_counter_issues_a_for_update_query(self, store_id):
        """The actual concurrency fix is a SELECT ... FOR UPDATE row lock
        on this location's MaitreDQueueCounter row (see Store.
        _lock_location_counter's docstring) -- that's what makes Postgres
        block a second concurrent request until the first commits. SQLite
        (this test suite's DB) has no real row-level locking and silently
        no-ops FOR UPDATE, so a genuine multi-threaded test against it
        can't validate the fix -- confirmed live while writing this: it
        produced real duplicate positions/numbers under SQLite, not
        because the fix is wrong, but because SQLite can't honor it.
        This checks the fix mechanically instead: that the code actually
        issues a `.with_for_update()` query, which DOES lock correctly
        under Postgres in production."""
        from sqlalchemy.orm import Query
        from app.core.db import SessionLocal

        calls = []
        original = Query.with_for_update

        def spy(self, *a, **kw):
            calls.append(True)
            return original(self, *a, **kw)

        store = Store(store_id)
        with patch.object(Query, "with_for_update", spy):
            with SessionLocal() as db:
                store._lock_location_counter(db, None)
                db.commit()
        assert calls

    def test_sequential_joins_never_collide_on_number_or_position(self, store_id):
        """Baseline correctness with no concurrency involved: rapid
        sequential joins must produce strictly unique, increasing
        numbers/positions -- the property the lock exists to preserve
        under real concurrent access in production."""
        results = [
            _md(store_id).handle_message(f"+9230000{i:04d}", "table for 2, it's Guest")
            for i in range(15)
        ]
        assert [r.queue_number for r in results] == list(range(1, 16))
        assert [r.position for r in results] == list(range(1, 16))


# ── Fix 4: stale queue entries auto-expire ──────────────────────────────────

class TestQueueExpiry:
    def test_stale_entry_expires_and_queue_moves_up(self, store_id):
        early = _md(store_id, now=FRIDAY_8PM)
        early.handle_message("+923000000001", "table for 2, it's Aman")

        # Bilal joins 80 minutes later -- still fresh at sweep time, unlike
        # Aman who'll be 100 minutes old by then.
        mid = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=80))
        mid.store = early.store
        mid.handle_message("+923000000002", "table for 2, it's Bilal")

        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=100))  # past the default 90-min timeout
        later.store = early.store
        result = later.expire_stale_entries()
        assert result.expired == 1
        remaining = later.store.list_queue(status="waiting")
        assert [e.name for e in remaining] == ["Bilal"]
        assert remaining[0].position == 1

    def test_recent_entry_is_not_expired(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=30))
        later.store = md.store
        result = later.expire_stale_entries()
        assert result.expired == 0

    def test_expired_guest_gets_a_courtesy_message(self, store_id):
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=100))
        later.store = md.store
        result = later.expire_stale_entries()
        assert any(phone == PHONE for phone, _ in result.outbound)


# ── Fix 5 & 6: branch-grouped, paginated queue listing ──────────────────────

class TestQueueListingFormatting:
    def test_multi_branch_listing_has_a_header_per_branch(self, store_id):
        _seed_two_locations(store_id)
        md = _md_multi(store_id)
        md.handle_message(PHONE, "table for 2 at New Blue Area, it's Ahmed")
        from app.agents.maitre_d.staff import format_queue
        text = format_queue(store_id)
        assert "New Blue Area:" in text

    def test_listing_caps_at_20_with_a_remainder_note(self, store_id):
        md = _md(store_id)
        for i in range(25):
            md.handle_message(f"+9230000{i:04d}", "table for 2, it's Guest")
        from app.agents.maitre_d.staff import format_queue
        text = format_queue(store_id)
        assert text.count("•") == 20
        assert "5 more waiting" in text


# ── Fix 7: modifying an existing queue entry ────────────────────────────────

class TestModifyQueueEntry:
    def test_modify_party_size_while_waiting(self, store_id):
        md = _md(store_id)
        r1 = md.handle_message(PHONE, "table for 2, it's Ahmed")
        r2 = md.handle_message(PHONE, "actually we're 5 now")
        assert r2.action == "modified"
        assert r2.queue_number == r1.queue_number
        entry = md.store.latest_waiting_entry_for(PHONE)
        assert entry.party_size == 5

    def test_modify_with_no_active_entry_is_graceful(self, store_id):
        md = _md(store_id)
        r = md.handle_message(PHONE, "actually we're 5 now")
        assert r.action == "noop"

    def test_modify_does_not_hijack_an_active_fresh_booking_flow(self, store_id):
        """A slot-filling answer during a FRESH booking must continue
        THAT flow, never get intercepted as an update to some other
        entry -- even though "actually we're 5" classifies as "modify"
        intent on its own (see nlu.py's _fallback_intent)."""
        md = _md(store_id)
        r1 = md.handle_message(PHONE, "book")
        assert r1.action == "need_info"
        r2 = md.handle_message(PHONE, "actually we're 5")
        assert r2.action == "need_info"
        assert "name" in r2.text.lower()

    @staticmethod
    def _mock_llm_answering(answer: str) -> MagicMock:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = answer
        return mock_client

    def test_modify_message_routes_through_gateway_without_active_flow(self, store_id):
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, _entrance_text(store_id), store_id)
        handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        with patch("app.core.llm.get_client", return_value=self._mock_llm_answering("YES")):
            reply = handle_customer_for_store(PHONE, "actually we're 5 now", store_id)
        assert "updated" in reply.lower()

    def test_unrelated_message_with_no_queue_entry_never_reaches_maitre_d(self, store_id):
        """Regression, found via review: the modify trigger words ("change",
        "update", "actually", "make it") are ordinary English a customer
        with NO queue entry at all would plausibly use with the community
        agent ("can you update my phone number", "actually never mind").
        Without checking for an actual waiting entry first, every one of
        those got hijacked into maitre_d's "no active queue entry" reply
        instead of ever reaching the community agent. (No LLM mock needed
        here -- the has-an-entry check short-circuits before ever
        reaching the LLM classification.)"""
        from app.gateway.customer import handle_customer_for_store
        with patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md:
            handle_customer_for_store(PHONE, "can you update my phone number", store_id)
        mock_md.assert_not_called()

    def test_llm_confirms_genuine_modify_intent(self, store_id):
        from app.gateway.customer import _llm_confirms_modify_intent
        with patch("app.core.llm.get_client", return_value=self._mock_llm_answering("YES")):
            assert _llm_confirms_modify_intent("actually we're 5 now") is True

    def test_llm_rejects_unrelated_message_from_an_already_queued_guest(self, store_id):
        """The exact residual gap this LLM check exists to close: a guest
        who IS in the queue asking for something unrelated that happens
        to contain a trigger word ("actually") must not be misread as a
        request to change their party size."""
        from app.gateway.customer import _llm_confirms_modify_intent
        with patch("app.core.llm.get_client", return_value=self._mock_llm_answering("NO")):
            assert _llm_confirms_modify_intent("actually can I get extra napkins") is False

    def test_llm_failure_fails_closed(self, store_id):
        from app.gateway.customer import _llm_confirms_modify_intent
        with patch("app.core.llm.get_client", side_effect=RuntimeError("boom")):
            assert _llm_confirms_modify_intent("actually we're 5 now") is False

    def test_already_queued_guest_asking_something_unrelated_reaches_community_agent(self, store_id):
        """End-to-end version of the residual gap: even with an active
        queue entry, a message the LLM correctly reads as unrelated must
        still reach the community agent, not get treated as a modify."""
        from app.gateway.customer import handle_customer_for_store
        handle_customer_for_store(PHONE, _entrance_text(store_id), store_id)
        handle_customer_for_store(PHONE, "it's Ahmed, party of 2", store_id)
        with (
            patch("app.core.llm.get_client", return_value=self._mock_llm_answering("NO")),
            patch("app.agents.maitre_d.agent.get_maitre_d") as mock_md,
        ):
            handle_customer_for_store(PHONE, "actually can I get extra napkins", store_id)
        mock_md.assert_not_called()


# ── Fix 8: seated-grace / queue-timeout are per-store configurable ─────────

class TestConfigurableThresholds:
    def test_seated_grace_is_configurable(self, store_id):
        from app.agents.maitre_d.config import get_seated_grace_minutes, set_seated_grace_minutes
        assert get_seated_grace_minutes(store_id) == 120
        set_seated_grace_minutes(store_id, 30)
        assert get_seated_grace_minutes(store_id) == 30

    def test_queue_stale_minutes_is_configurable(self, store_id):
        from app.agents.maitre_d.config import get_queue_stale_minutes, set_queue_stale_minutes
        assert get_queue_stale_minutes(store_id) == 90
        set_queue_stale_minutes(store_id, 45)
        assert get_queue_stale_minutes(store_id) == 45

    def test_staff_can_set_seated_grace_via_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        from app.agents.maitre_d.config import get_seated_grace_minutes
        reply = handle_internal_for_store("whatsapp:+923220000000", "seated grace 30", store_id)
        assert "30" in reply
        assert get_seated_grace_minutes(store_id) == 30

    def test_staff_can_set_queue_timeout_via_command(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        from app.agents.maitre_d.config import get_queue_stale_minutes
        reply = handle_internal_for_store("whatsapp:+923220000000", "queue timeout 45", store_id)
        assert "45" in reply
        assert get_queue_stale_minutes(store_id) == 45

    def test_queue_timeout_zero_is_rejected(self, store_id):
        """Regression, found via review: "queue timeout 0" (a plausible
        typo) would otherwise make the very next sweep instantly expire
        EVERY waiting guest with no warning."""
        from app.gateway.internal import handle_internal_for_store
        from app.agents.maitre_d.config import get_queue_stale_minutes
        reply = handle_internal_for_store("whatsapp:+923220000000", "queue timeout 0", store_id)
        assert "between" in reply.lower()
        assert get_queue_stale_minutes(store_id) == 90  # unchanged

    def test_seated_grace_absurdly_high_value_is_rejected(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        from app.agents.maitre_d.config import get_seated_grace_minutes
        reply = handle_internal_for_store("whatsapp:+923220000000", "seated grace 999999", store_id)
        assert "between" in reply.lower()
        assert get_seated_grace_minutes(store_id) == 120  # unchanged

    def test_shortened_seated_grace_actually_affects_the_guard(self, store_id):
        from app.agents.maitre_d.config import set_seated_grace_minutes
        set_seated_grace_minutes(store_id, 10)
        md = _md(store_id, now=FRIDAY_8PM)
        md.handle_message(PHONE, "table for 2, it's Ahmed")
        Store(store_id).admit_next(None, now=FRIDAY_8PM)

        later = _md(store_id, now=FRIDAY_8PM + timedelta(minutes=20))  # past the shortened 10-min grace
        later.store = md.store
        r = later.handle_message(PHONE, "table for 2, it's Ahmed")
        assert r.action == "queued"  # would be "already_seated" under the default 120-min grace


# ── Fix 9: staff can add a walk-in without a phone number ──────────────────

class TestWalkInWithoutPhone:
    def test_add_position_without_phone(self, store_id):
        from app.agents.maitre_d.staff import insert_queue_position
        md = _md(store_id)
        md.handle_message("+923000000001", "table for 2, it's Aman")
        reply = insert_queue_position(store_id, "1 Walked In, party 3")
        assert "Added Walked In" in reply
        assert "no phone" in reply.lower()
        entries = md.store.list_queue(status="waiting")
        assert entries[0].name == "Walked In"
        assert entries[0].phone == ""
        assert entries[0].party_size == 3

    def test_add_position_with_phone_still_works(self, store_id):
        from app.agents.maitre_d.staff import insert_queue_position
        reply = insert_queue_position(store_id, "1 +923009998888 Ali Khan, party 2")
        assert "Added Ali Khan" in reply
        assert "no phone" not in reply.lower()

    def test_phone_less_walk_in_is_silently_skipped_on_seating_notification(self, store_id):
        from app.agents.maitre_d.staff import insert_queue_position, admit_next_in_queue
        insert_queue_position(store_id, "1 Walked In, party 2")
        with patch("app.core.outbound.send_from_store") as mock_send:
            reply = admit_next_in_queue(store_id)
        assert "Admitted" in reply
        mock_send.assert_not_called()  # no phone on file -- nothing to send

    def test_walk_in_created_at_uses_venue_local_clock(self, store_id):
        """Regression (found via live testing on the real deployment):
        insert_queue_position used to let QueueEntry's created_at default
        to datetime.now() (server clock) instead of the venue-local clock
        it's compared against elsewhere -- a walk-in inserted seconds ago
        was immediately flagged as stale by the maintenance sweep, because
        its created_at read hours behind Asia/Karachi's "now" on a
        UTC-clocked server."""
        from app.agents.maitre_d.staff import insert_queue_position
        from app.agents.maitre_d.config import VenueConfig
        insert_queue_position(store_id, "1 Walked In, party 2")
        entry = Store(store_id).list_queue(status="waiting")[0]
        venue_now = VenueConfig.load(store_id).now()
        assert abs((venue_now - entry.created_at).total_seconds()) < 30
