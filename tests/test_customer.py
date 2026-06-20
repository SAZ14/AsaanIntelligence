"""Tests for the WhatsApp loyalty stamp-card engine (Customer agent).

Properties, not answer keys: stamp counts derive from the configured tiers, so
the tests assert behaviour (a card fills, completes, promotes, persists) rather
than hardcoded totals. No LLM and no network are involved anywhere.
"""

from __future__ import annotations

from app.agents.customer import (
    DEFAULT_TIERS,
    InMemoryCardStore,
    JsonCardStore,
    LoyaltyProgram,
    Registry,
    Tier,
    build_restaurant,
    build_wa_link,
    format_card_status,
    load_registry,
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


def test_load_registry_gives_each_restaurant_its_own_store(tmp_path):
    config = tmp_path / "restaurants.json"
    config.write_text(
        '[{"id":"a","name":"Cafe A","whatsapp_number":"+111","reward":"a free A"},'
        ' {"id":"b","name":"Cafe B","whatsapp_number":"+222","reward":"a free B"}]'
    )
    reg = load_registry(config, store_dir=tmp_path / "stores")
    reg.by_id("a").program.record_scan("+923001234567")

    # Persisted to a per-restaurant file; the other venue stays empty.
    assert (tmp_path / "stores" / "a.json").exists()
    assert not (tmp_path / "stores" / "b.json").exists()
    reg2 = load_registry(config, store_dir=tmp_path / "stores")
    assert reg2.by_id("a").program.lookup("+923001234567").stamps == 1
    assert reg2.by_id("b").program.lookup("+923001234567") is None
