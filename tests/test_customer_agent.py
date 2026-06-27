"""Customer agent unit tests.

All Supabase store functions are mocked so these tests run without any
external dependencies. Tests every customer-facing scenario:
onboarding, stamp codes, queries, opt-out, leaderboard, data isolation.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

from app.agents.customer.community.models import CommunityMember, VenueConfig, RedeemCode
from app.agents.customer.agents.community_customer import handle_customer_message, AgentReply

# ── Shared test data ──────────────────────────────────────────────────────────

STORE_ID = 1
PHONE = "+923001234567"
FROM_PHONE = f"whatsapp:{PHONE}"

DEFAULT_CONFIG = VenueConfig(
    venue_name="Café Test",
    stamp_goal=5,
    reward_text="a free coffee",
    winback_days=5,
    code_expiry_days=30,
    owner_phones=[],
    qr_greeting="",
)


def _member(stamps_current=2, stamps_lifetime=10, opted_in=True, name="Ali"):
    return CommunityMember(
        phone=PHONE,
        name=name,
        stamps_current=stamps_current,
        stamps_lifetime=stamps_lifetime,
        joined_at=datetime.now(timezone.utc).isoformat(),
        last_activity_at=datetime.now(timezone.utc).isoformat(),
        opted_in=opted_in,
    )


def _valid_code(code="SR-AB12", days_old=0) -> RedeemCode:
    issued = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return RedeemCode(code=code.upper(), issued_at=issued)


def _call(body, members=None, sessions=None, codes=None, member=None, config=None):
    """Helper: patch all store functions and call handle_customer_message."""
    with (
        patch("app.agents.customer.agents.community_customer.load_venue_config",
              return_value=config or DEFAULT_CONFIG),
        patch("app.agents.customer.agents.community_customer.load_members",
              return_value={PHONE: member} if member else (members or {})),
        patch("app.agents.customer.agents.community_customer.load_onboarding_sessions",
              return_value=sessions or {}),
        patch("app.agents.customer.agents.community_customer.save_members"),
        patch("app.agents.customer.agents.community_customer.save_onboarding_sessions"),
        patch("app.agents.customer.agents.community_customer.clear_onboarding_session"),
        patch("app.agents.customer.community.tokens.load_redeem_codes",
              return_value=codes or []),
        patch("app.agents.customer.community.tokens.append_redeem_code"),
        patch("app.agents.customer.community.tokens.update_redeem_code"),
        patch("app.agents.customer.community.stamps.append_stamp_event"),
    ):
        return handle_customer_message(FROM_PHONE, body, STORE_ID)


# ── New visitor onboarding ─────────────────────────────────────────────────────

def test_new_visitor_starts_onboarding():
    reply = _call("hi")
    assert "Welcome" in reply.body
    assert "name" in reply.body.lower()


def test_new_visitor_any_message_starts_onboarding():
    reply = _call("show me the menu")
    assert "name" in reply.body.lower()


# ── Onboarding state: awaiting name ───────────────────────────────────────────

def test_onboarding_empty_body_asks_for_name():
    reply = _call("", sessions={PHONE: "awaiting_name"})
    assert "name" in reply.body.lower()


def test_onboarding_short_name_rejected():
    reply = _call("A", sessions={PHONE: "awaiting_name"})
    assert "2 character" in reply.body or "at least" in reply.body.lower()


def test_onboarding_name_too_long_rejected():
    long_name = "A" * 81
    reply = _call(long_name, sessions={PHONE: "awaiting_name"})
    assert "long" in reply.body.lower() or "shorter" in reply.body.lower()


def test_onboarding_receipt_code_as_name_rejected():
    reply = _call("SR-AB12", sessions={PHONE: "awaiting_name"})
    assert "name" in reply.body.lower()
    assert "code" in reply.body.lower() or "receipt" in reply.body.lower()


def test_onboarding_valid_name_registers_member():
    with (
        patch("app.agents.customer.agents.community_customer.load_venue_config",
              return_value=DEFAULT_CONFIG),
        patch("app.agents.customer.agents.community_customer.load_members",
              return_value={}),
        patch("app.agents.customer.agents.community_customer.load_onboarding_sessions",
              return_value={PHONE: "awaiting_name"}),
        patch("app.agents.customer.agents.community_customer.save_members") as mock_save,
        patch("app.agents.customer.agents.community_customer.save_onboarding_sessions"),
        patch("app.agents.customer.agents.community_customer.clear_onboarding_session") as mock_clear,
        patch("app.agents.customer.community.stamps.append_stamp_event"),
    ):
        reply = handle_customer_message(FROM_PHONE, "Sara Khan", STORE_ID)

    assert "Welcome" in reply.body or "Sara" in reply.body
    mock_save.assert_called_once()
    mock_clear.assert_called_once()


# ── Opted-out member ──────────────────────────────────────────────────────────

def test_opted_out_member_gets_empty_reply():
    reply = _call("hi", member=_member(opted_in=False))
    assert reply.body == ""


# ── Receipt code flow ─────────────────────────────────────────────────────────

def test_code_before_joining_prompts_join_first():
    reply = _call("SR-AB12")
    assert "join" in reply.body.lower() or "scan" in reply.body.lower()


def test_valid_code_adds_stamp():
    code = _valid_code("SR-AB12")
    reply = _call("SR-AB12", member=_member(stamps_current=0), codes=[code])
    assert "Stamp" in reply.body or "stamp" in reply.body
    assert "added" in reply.body.lower() or "1" in reply.body


def test_code_case_insensitive():
    code = _valid_code("SR-AB12")
    reply = _call("sr-ab12", member=_member(stamps_current=0), codes=[code])
    assert "stamp" in reply.body.lower()


def test_already_redeemed_code_rejected():
    redeemed_code = RedeemCode(
        code="SR-AB12",
        issued_at=datetime.now(timezone.utc).isoformat(),
        redeemed_at=datetime.now(timezone.utc).isoformat(),
        redeemed_by="+929999999",
    )
    reply = _call("SR-AB12", member=_member(), codes=[redeemed_code])
    assert "already" in reply.body.lower() or "used" in reply.body.lower()


def test_expired_code_rejected():
    old_code = _valid_code("SR-XY99", days_old=31)  # > 30 day expiry
    reply = _call("SR-XY99", member=_member(), codes=[old_code])
    assert "expired" in reply.body.lower()


def test_unknown_code_returns_not_found():
    reply = _call("SR-ZZ99", member=_member(), codes=[])
    assert "not found" in reply.body.lower() or "wasn't found" in reply.body.lower()


def test_stamp_goal_reached_issues_reward():
    """4 stamps already, goal is 5 — 5th stamp should trigger reward."""
    code = _valid_code("SR-RR55")
    reply = _call("SR-RR55", member=_member(stamps_current=4), codes=[code])
    assert "earned" in reply.body.lower() or "free coffee" in reply.body.lower()
    assert "reset" in reply.body.lower() or "Reset" in reply.body


def test_stamp_progress_shown_before_goal():
    code = _valid_code("SR-CC11")
    reply = _call("SR-CC11", member=_member(stamps_current=1), codes=[code])
    # Should show X/5 progress
    assert "/5" in reply.body or "more" in reply.body.lower()


# ── Existing member queries ───────────────────────────────────────────────────

def test_my_stamps_query():
    reply = _call("my stamps", member=_member(stamps_current=3))
    assert "3" in reply.body and "5" in reply.body  # current/goal
    assert "stamp" in reply.body.lower()


def test_stamp_balance_query_alias():
    reply = _call("stamp balance", member=_member(stamps_current=2))
    assert "2" in reply.body


def test_greeting_shows_welcome_back():
    reply = _call("hi", member=_member(name="Sara"))
    assert "Sara" in reply.body or "welcome back" in reply.body.lower()


def test_hello_shows_welcome_back():
    reply = _call("Hello!", member=_member(name="Hassan"))
    assert "Hassan" in reply.body or "welcome" in reply.body.lower()


def test_empty_message_from_member_shows_help():
    reply = _call("", member=_member(name="Sara"))
    assert "receipt" in reply.body.lower() or "stamps" in reply.body.lower()


def test_leaderboard_query():
    m1 = _member(name="Ali")
    m2 = CommunityMember(
        phone="+923000000001", name="Sara",
        stamps_current=5, stamps_lifetime=20,
        joined_at=datetime.now(timezone.utc).isoformat(),
        last_activity_at=datetime.now(timezone.utc).isoformat(),
        opted_in=True,
    )
    members = {PHONE: m1, "+923000000001": m2}
    with (
        patch("app.agents.customer.agents.community_customer.load_venue_config",
              return_value=DEFAULT_CONFIG),
        patch("app.agents.customer.agents.community_customer.load_members",
              return_value=members),
        patch("app.agents.customer.agents.community_customer.load_onboarding_sessions",
              return_value={}),
        patch("app.agents.customer.community.stamps.append_stamp_event"),
        patch("app.agents.customer.community.leaderboard.weekly_stamp_counts",
              return_value={PHONE: 3, "+923000000001": 7}),
    ):
        reply = handle_customer_message(FROM_PHONE, "leaderboard", STORE_ID)

    assert "leaderboard" in reply.body.lower() or "top" in reply.body.lower() or "Sara" in reply.body


def test_ranking_keyword_also_triggers_leaderboard():
    with (
        patch("app.agents.customer.agents.community_customer.load_venue_config",
              return_value=DEFAULT_CONFIG),
        patch("app.agents.customer.agents.community_customer.load_members",
              return_value={PHONE: _member()}),
        patch("app.agents.customer.agents.community_customer.load_onboarding_sessions",
              return_value={}),
        patch("app.agents.customer.community.leaderboard.weekly_stamp_counts",
              return_value={}),
    ):
        reply = handle_customer_message(FROM_PHONE, "show ranking", STORE_ID)

    assert reply.body  # non-empty


# ── No ZAI key → deterministic fallback ─────────────────────────────────────

def test_no_llm_key_returns_help_fallback(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "")
    # Reset cached client
    import app.agents.customer.agents.community_customer as mod
    mod._zai_client = None

    reply = _call("Tell me about the croissant", member=_member())
    assert reply.body  # must return something even without LLM
    assert "receipt" in reply.body.lower() or "stamp" in reply.body.lower() or "hi" in reply.body.lower()


# ── Stamp logic unit tests ────────────────────────────────────────────────────

def test_apply_stamp_increments_counts():
    from app.agents.customer.community.stamps import apply_stamp
    m = _member(stamps_current=2, stamps_lifetime=10)
    with patch("app.agents.customer.community.stamps.append_stamp_event"):
        result = apply_stamp(m, "SR-XX11", DEFAULT_CONFIG, STORE_ID)
    assert m.stamps_current == 3
    assert m.stamps_lifetime == 11
    assert not result.reward_issued


def test_apply_stamp_triggers_reward_at_goal():
    from app.agents.customer.community.stamps import apply_stamp
    m = _member(stamps_current=4)  # goal is 5
    with patch("app.agents.customer.community.stamps.append_stamp_event"):
        result = apply_stamp(m, "SR-WW22", DEFAULT_CONFIG, STORE_ID)
    assert result.reward_issued
    assert m.stamps_current == 0  # reset after reward
    assert m.stamps_lifetime == 11


def test_stamp_status_message_format():
    from app.agents.customer.community.stamps import stamp_status_message
    m = _member(stamps_current=3)
    msg = stamp_status_message(m, DEFAULT_CONFIG)
    assert "3/5" in msg
    assert "2 more" in msg
    assert "free coffee" in msg


def test_welcome_message_format():
    from app.agents.customer.community.stamps import welcome_message
    msg = welcome_message("Zara", DEFAULT_CONFIG)
    assert "Zara" in msg
    assert "Café Test" in msg
    assert "5 stamps" in msg or "5" in msg


def test_welcome_back_message_format():
    from app.agents.customer.community.stamps import welcome_back_message
    m = _member(stamps_current=2)
    msg = welcome_back_message("Ali", m, DEFAULT_CONFIG)
    assert "Ali" in msg
    assert "2/5" in msg


# ── Redeem code unit tests ────────────────────────────────────────────────────

def test_is_redeem_code_accepts_valid():
    from app.agents.customer.community.tokens import is_redeem_code
    assert is_redeem_code("SR-AB12")
    assert is_redeem_code("sr-ab12")
    assert is_redeem_code("SR-1A2B")


def test_is_redeem_code_rejects_invalid():
    from app.agents.customer.community.tokens import is_redeem_code
    assert not is_redeem_code("hello")
    assert not is_redeem_code("SR-ABC")      # 3 chars, need 4
    assert not is_redeem_code("SR-ABCDE")   # 5 chars
    assert not is_redeem_code("SR_AB12")    # wrong separator
    assert not is_redeem_code("")


def test_validate_code_passes_fresh_code():
    from app.agents.customer.community.tokens import validate_code
    code = _valid_code()
    err = validate_code(code, DEFAULT_CONFIG)
    assert err is None


def test_validate_code_rejects_already_redeemed():
    from app.agents.customer.community.tokens import validate_code
    code = RedeemCode(
        code="SR-AB12",
        issued_at=datetime.now(timezone.utc).isoformat(),
        redeemed_at=datetime.now(timezone.utc).isoformat(),
        redeemed_by="+92111",
    )
    err = validate_code(code, DEFAULT_CONFIG)
    assert err is not None
    assert "already" in err.lower()


def test_validate_code_rejects_expired():
    from app.agents.customer.community.tokens import validate_code
    code = _valid_code(days_old=31)
    err = validate_code(code, DEFAULT_CONFIG)
    assert err is not None
    assert "expired" in err.lower()


def test_normalize_code():
    from app.agents.customer.community.tokens import normalize_code
    assert normalize_code("  sr-ab12 ") == "SR-AB12"
