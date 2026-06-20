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
    Tier,
    build_wa_link,
    format_card_status,
)
from app.whatsapp import WhatsAppNotifier
from app.whatsapp.webhook import process_scan, twiml_reply


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


# ── Completing a card unlocks a reward and promotes ──

def test_completing_card_unlocks_reward_and_promotes():
    prog = _program()
    silver = DEFAULT_TIERS[0]
    result = None
    for _ in range(silver.stamps_required):
        result = prog.record_scan(PHONE)

    # Last scan completed the Silver card.
    assert result.completed_tier == silver
    assert result.promoted_to == DEFAULT_TIERS[1]
    # Fresh card has reset to zero stamps on the new (Gold) tier.
    assert result.card.stamps == 0
    assert result.card.tier_index == 1
    # Exactly one reward unlocked, not yet redeemed.
    assert len(result.card.rewards) == 1
    assert result.card.rewards[0].reward == silver.reward
    assert result.card.rewards[0].redeemed is False
    # Message congratulates + introduces the better tier.
    assert silver.reward.upper() in result.message
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
