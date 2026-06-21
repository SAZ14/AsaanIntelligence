"""Tests for the WhatsApp loyalty stamp-card engine (Customer agent).

Properties, not answer keys: stamp counts derive from the configured tiers, so
the tests assert behaviour (a card fills, completes, promotes, persists) rather
than hardcoded totals. No LLM and no network are involved anywhere.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.agents.customer import (
    DEFAULT_INACTIVE_DAYS,
    DEFAULT_LOYAL_MIN_SCANS,
    DEFAULT_TIERS,
    EngagementCandidate,
    InMemoryCardStore,
    JsonCardStore,
    LoyaltyCard,
    LoyaltyProgram,
    Registry,
    SqliteCardStore,
    Tier,
    at_risk_loyal_customers,
    build_restaurant,
    build_wa_link,
    days_since_last_scan,
    format_card_status,
    format_event_invite,
    format_miss_you_message,
    load_registry,
    send_event_invites,
    send_reengagement,
    top_loyal_customers,
)
from app.whatsapp import WhatsAppNotifier
from app.whatsapp.webhook import process_scan, route, twiml_reply


PHONE = "+923001234567"


def _program(store=None) -> LoyaltyProgram:
    return LoyaltyProgram(venue_name="Sugar Rush", store=store or InMemoryCardStore())


# ── First scan creates a card ──

def test_first_scan_creates_card_with_one_stamp():
    prog = _program()
    result = prog.record_scan(PHONE)

    assert result.is_first_scan
    assert result.card.stamps == 1
    assert result.card.total_scans == 1
    assert result.completed_tier is None
    # Welcome message shows progress toward the first reward.
    assert "Welcome" in result.message
    assert DEFAULT_TIERS[0].reward in result.message
    assert "1/5" in result.message


def test_each_scan_adds_one_stamp():
    prog = _program()
    for expected in range(1, DEFAULT_TIERS[0].stamps_required):  # stop before completion
        result = prog.record_scan(PHONE)
        assert result.card.stamps == expected
        assert result.completed_tier is None


# ── Default is a single tier that loops ──

def test_default_single_tier_completes_and_resets():
    prog = _program()
    tier = DEFAULT_TIERS[0]
    assert len(DEFAULT_TIERS) == 1   # default programme is intentionally simple

    result = None
    for _ in range(tier.stamps_required):
        result = prog.record_scan(PHONE)

    # Completing the card unlocks the reward and starts a fresh one (loops).
    assert result.completed_tier == tier
    assert result.promoted_to == tier            # same tier, reset
    assert result.card.stamps == 0
    assert result.card.tier_index == 0
    assert len(result.card.rewards) == 1
    assert result.card.rewards[0].redeemed is False
    assert tier.reward.upper() in result.message
    assert "reset" in result.message


# ── Multi-tier ladders still work when configured ──

def test_completing_card_promotes_when_multi_tier():
    prog = LoyaltyProgram(tiers=[
        Tier("Silver", 3, "a free coffee"),
        Tier("Gold", 5, "a free cake"),
    ])
    result = None
    for _ in range(3):
        result = prog.record_scan(PHONE)
    assert result.completed_tier.name == "Silver"
    assert result.promoted_to.name == "Gold"
    assert result.card.tier_index == 1
    assert "Gold" in result.message


def test_top_tier_loops_without_error():
    # A tiny single-tier programme: every completion resets the same card.
    prog = LoyaltyProgram(tiers=[Tier("Solo", 2, "a free coffee")])
    completions = 0
    for _ in range(7):
        r = prog.record_scan(PHONE)
        if r.completed_tier is not None:
            completions += 1
            assert r.promoted_to.name == "Solo"   # loops on itself
            assert "reset" in r.message
    assert completions == 3   # 7 scans / 2 per card


# ── Identity is the phone number ──

def test_different_numbers_are_different_customers():
    prog = _program()
    prog.record_scan("+920000000001")
    prog.record_scan("+920000000002")
    prog.record_scan("+920000000002")

    a = prog.lookup("+920000000001")
    b = prog.lookup("+920000000002")
    assert a.stamps == 1
    assert b.stamps == 2
    assert len(prog.store.all()) == 2


# ── Staff redemption ──

def test_redeem_marks_reward_given():
    prog = _program()
    for _ in range(DEFAULT_TIERS[0].stamps_required):
        prog.record_scan(PHONE)

    card = prog.lookup(PHONE)
    assert len(prog.pending_rewards(card)) == 1

    given = prog.redeem(PHONE)
    assert given is not None and given.redeemed is True
    assert prog.pending_rewards(prog.lookup(PHONE)) == []

    status = format_card_status(prog.lookup(PHONE), prog)
    assert "No rewards pending" in status


# ── Persistence (the store the webhook uses) ──

def test_json_store_persists_across_program_instances(tmp_path):
    store_path = tmp_path / "cards.json"
    prog1 = _program(JsonCardStore(store_path))
    for _ in range(3):
        prog1.record_scan(PHONE)

    # A brand-new program reading the same file sees the saved progress.
    prog2 = _program(JsonCardStore(store_path))
    card = prog2.lookup(PHONE)
    assert card is not None
    assert card.stamps == 3
    assert card.total_scans == 3


# ── wa.me link ──

def test_wa_link_is_well_formed():
    link = build_wa_link("whatsapp:+1 415 523 8886", "hi there")
    assert link.startswith("https://wa.me/14155238886?text=")
    assert "hi%20there" in link


# ── Webhook glue (framework-agnostic core) ──

def test_process_scan_returns_reply_and_strips_whatsapp_prefix():
    prog = _program()
    reply = process_scan("whatsapp:+923009999999", prog)
    assert "Welcome" in reply
    assert prog.lookup("+923009999999") is not None   # prefix was stripped


def test_twiml_reply_escapes_and_wraps():
    xml = twiml_reply("a & b")
    assert xml.startswith("<?xml")
    assert "<Message>a &amp; b</Message>" in xml


# ── Delivery stays green in DRY_RUN ──

def test_notifier_dry_run_sends_loyalty_message():
    prog = _program()
    n = WhatsAppNotifier(dry_run=True)
    result = prog.record_scan(PHONE)
    msg = n.send(PHONE, result.message)
    assert msg.status == "dry_run"
    assert msg.to == "whatsapp:+923001234567"


# ── Re-engagement & VIP / events ──

TODAY = date(2026, 6, 20)


def _card(phone, scans, days_since_scan, nudged_days_ago=None) -> LoyaltyCard:
    c = LoyaltyCard(
        phone=phone, stamps=1, total_scans=scans,
        created_at=(TODAY - timedelta(days=days_since_scan)).isoformat(),
        updated_at=(TODAY - timedelta(days=days_since_scan)).isoformat(),
    )
    if nudged_days_ago is not None:
        c.last_nudged_at = (TODAY - timedelta(days=nudged_days_ago)).isoformat()
    return c


def _seeded_program(*cards) -> LoyaltyProgram:
    prog = _program()
    for c in cards:
        prog.store.put(c)
    return prog


def test_days_since_last_scan():
    assert days_since_last_scan(_card("+1", 5, 7), today=TODAY) == 7
    assert days_since_last_scan(LoyaltyCard(phone="+1"), today=TODAY) is None


def test_at_risk_selects_loyal_inactive_not_recently_nudged():
    loyal_quiet = _card("+loyalquiet", scans=6, days_since_scan=7)        # ✓ include
    loyal_recent = _card("+loyalrecent", scans=6, days_since_scan=1)      # ✗ still active
    not_loyal = _card("+notloyal", scans=1, days_since_scan=10)           # ✗ not loyal
    nudged = _card("+nudged", scans=6, days_since_scan=7, nudged_days_ago=2)  # ✗ cooldown
    prog = _seeded_program(loyal_quiet, loyal_recent, not_loyal, nudged)

    cands = at_risk_loyal_customers(prog, min_scans=3, inactive_days=5,
                                    cooldown_days=5, today=TODAY)
    assert [c.card.phone for c in cands] == ["+loyalquiet"]
    assert cands[0].days_inactive == 7
    assert "miss you" in cands[0].message.lower()


def test_at_risk_sorted_most_loyal_first():
    prog = _seeded_program(
        _card("+a", scans=4, days_since_scan=8),
        _card("+b", scans=12, days_since_scan=8),
        _card("+c", scans=7, days_since_scan=8),
    )
    cands = at_risk_loyal_customers(prog, today=TODAY)
    assert [c.card.phone for c in cands] == ["+b", "+c", "+a"]


def test_send_reengagement_marks_and_prevents_respam():
    prog = _seeded_program(_card("+loyalquiet", scans=6, days_since_scan=7))
    n = WhatsAppNotifier(dry_run=True)

    cands = at_risk_loyal_customers(prog, today=TODAY)
    sent = send_reengagement(prog, n, cands, today=TODAY)
    assert len(sent) == 1 and sent[0].status == "dry_run"

    # Now marked as nudged today → a re-run finds nobody (cooldown).
    assert prog.lookup("+loyalquiet").last_nudged_at == TODAY.isoformat()
    assert at_risk_loyal_customers(prog, today=TODAY) == []


def test_top_loyal_customers_ranked_by_scans():
    prog = _seeded_program(
        _card("+a", scans=4, days_since_scan=1),
        _card("+b", scans=20, days_since_scan=1),
        _card("+c", scans=9, days_since_scan=1),
    )
    tops = top_loyal_customers(prog, n=2)
    assert [c.phone for c in tops] == ["+b", "+c"]


def test_event_invite_send_and_format():
    prog = _seeded_program(_card("+vip", scans=15, days_since_scan=1))
    n = WhatsAppNotifier(dry_run=True)
    sent = send_event_invites(n, "Sugar Rush", top_loyal_customers(prog, 5),
                              "Tasting night Friday 7pm.")
    assert len(sent) == 1
    body = sent[0].body
    assert "invited" in body and "Tasting night Friday 7pm." in body
    assert "YES" in format_event_invite("Sugar Rush", "x")


def test_engagement_policy_is_per_restaurant_from_config(tmp_path):
    config = tmp_path / "restaurants.json"
    config.write_text(
        '[{"id":"a","name":"A","whatsapp_number":"+111","inactive_days":10,'
        ' "min_scans":6,"nudge_cooldown_days":3},'
        ' {"id":"b","name":"B","whatsapp_number":"+222"}]'
    )
    reg = load_registry(config, db_path=tmp_path / "l.db")
    a, b = reg.by_id("a"), reg.by_id("b")
    # Venue A uses the owner-set values; B falls back to the defaults.
    assert (a.inactive_days, a.min_scans, a.nudge_cooldown_days) == (10, 6, 3)
    assert b.inactive_days == DEFAULT_INACTIVE_DAYS
    assert b.min_scans == DEFAULT_LOYAL_MIN_SCANS


def test_last_nudged_at_persists_in_sqlite(tmp_path):
    db = tmp_path / "loyalty.db"
    store = SqliteCardStore(db, "x")
    card = _card("+vip", scans=6, days_since_scan=7)
    card.last_nudged_at = TODAY.isoformat()
    store.put(card)

    reloaded = SqliteCardStore(db, "x").get("+vip")
    assert reloaded.last_nudged_at == TODAY.isoformat()


def test_sqlite_migrates_old_db_without_last_nudged_at(tmp_path):
    # Simulate a db created before the column existed.
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE cards (restaurant_id TEXT, phone TEXT, tier_index INTEGER, "
        "stamps INTEGER, total_scans INTEGER, created_at TEXT, updated_at TEXT, "
        "rewards TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (restaurant_id, phone))"
    )
    conn.execute(
        "INSERT INTO cards VALUES ('x','+old',0,2,2,'2026-06-10','2026-06-10','[]')"
    )
    conn.commit()
    conn.close()

    # Opening through SqliteCardStore should add the column and still work.
    store = SqliteCardStore(db, "x")
    card = store.get("+old")
    assert card is not None and card.last_nudged_at is None
    card.last_nudged_at = TODAY.isoformat()
    store.put(card)
    assert SqliteCardStore(db, "x").get("+old").last_nudged_at == TODAY.isoformat()


# ── Multi-restaurant: separate QRs, separate tracking ──

def _registry() -> Registry:
    return Registry([
        build_restaurant("sugar_rush", "Sugar Rush", "+14155238886",
                         stamps_required=5, reward="a free ice cream"),
        build_restaurant("burger_lab", "Burger Lab", "+14155551234",
                         stamps_required=5, reward="a free burger"),
    ])


def test_registry_routes_by_to_number():
    reg = _registry()
    assert reg.by_number("whatsapp:+1 415 523 8886").id == "sugar_rush"
    assert reg.by_number("+14155551234").id == "burger_lab"
    assert reg.by_number("+10000000000") is None


def test_each_restaurant_has_its_own_qr_and_reward():
    reg = _registry()
    sr, bl = reg.by_id("sugar_rush"), reg.by_id("burger_lab")
    assert sr.wa_link() != bl.wa_link()
    assert "14155238886" in sr.wa_link()
    assert "a free ice cream" in sr.reward_line()
    assert "a free burger" in bl.reward_line()


def test_same_customer_tracked_independently_per_restaurant():
    reg = _registry()
    # Customer scans at Sugar Rush twice, Burger Lab once — routed by To number.
    route("whatsapp:+923001234567", "+14155238886", reg)
    route("whatsapp:+923001234567", "+14155238886", reg)
    route("whatsapp:+923001234567", "+14155551234", reg)

    assert reg.by_id("sugar_rush").program.lookup("+923001234567").stamps == 2
    assert reg.by_id("burger_lab").program.lookup("+923001234567").stamps == 1


def test_unknown_number_is_handled_gracefully():
    reg = _registry()
    reply = route("whatsapp:+923001234567", "+19998887777", reg)
    assert "isn't set up" in reply


def test_load_registry_shares_one_sqlite_db_isolated_per_venue(tmp_path):
    config = tmp_path / "restaurants.json"
    config.write_text(
        '[{"id":"a","name":"Cafe A","whatsapp_number":"+111","reward":"a free A"},'
        ' {"id":"b","name":"Cafe B","whatsapp_number":"+222","reward":"a free B"}]'
    )
    db = tmp_path / "loyalty.db"
    reg = load_registry(config, db_path=db)
    reg.by_id("a").program.record_scan("+923001234567")

    # One shared database file; the venues stay isolated within it.
    assert db.exists()
    reg2 = load_registry(config, db_path=db)
    assert reg2.by_id("a").program.lookup("+923001234567").stamps == 1
    assert reg2.by_id("b").program.lookup("+923001234567") is None


# ── SQLite store (the production storage) ──

def test_sqlite_store_persists_across_program_instances(tmp_path):
    db = tmp_path / "loyalty.db"
    p1 = LoyaltyProgram(store=SqliteCardStore(db, "sugar_rush"))
    for _ in range(3):
        p1.record_scan(PHONE)

    # A fresh program reading the same db file sees the saved progress.
    p2 = LoyaltyProgram(store=SqliteCardStore(db, "sugar_rush"))
    card = p2.lookup(PHONE)
    assert card is not None and card.stamps == 3 and card.total_scans == 3


def test_sqlite_separates_restaurants_in_one_db(tmp_path):
    db = tmp_path / "loyalty.db"
    a = LoyaltyProgram(store=SqliteCardStore(db, "a"))
    b = LoyaltyProgram(store=SqliteCardStore(db, "b"))
    a.record_scan(PHONE)
    a.record_scan(PHONE)
    b.record_scan(PHONE)

    assert a.lookup(PHONE).stamps == 2
    assert b.lookup(PHONE).stamps == 1
    assert {c.phone for c in a.store.all()} == {PHONE}  # only its own venue's cards


def test_sqlite_preserves_earned_rewards(tmp_path):
    db = tmp_path / "loyalty.db"
    p1 = LoyaltyProgram(store=SqliteCardStore(db, "x"))  # default single tier, 5 stamps
    for _ in range(DEFAULT_TIERS[0].stamps_required):
        p1.record_scan(PHONE)

    p2 = LoyaltyProgram(store=SqliteCardStore(db, "x"))
    card = p2.lookup(PHONE)
    assert len(card.rewards) == 1
    assert card.rewards[0].reward == DEFAULT_TIERS[0].reward
    assert card.rewards[0].redeemed is False
    # Redemption persists too.
    p2.redeem(PHONE)
    assert LoyaltyProgram(store=SqliteCardStore(db, "x")).pending_rewards(p2.lookup(PHONE)) == []


def test_constructing_sqlite_store_touches_no_disk(tmp_path):
    # Building a store (e.g. for QR generation) must not create the db file.
    db = tmp_path / "loyalty.db"
    SqliteCardStore(db, "a")
    assert not db.exists()
