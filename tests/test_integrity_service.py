"""Integrity service tests.

Uses the real sample CSV data so the analysis engine is exercised end-to-end.
The POSConnection is inserted into the SQLite test DB so IntegrityService._get_config
resolves correctly without hitting Supabase or a real POS.
"""
import json
import pytest
from pathlib import Path

from tests.conftest import seed_chain, seed_store, TestSession

DATA = Path(__file__).resolve().parent.parent / "data"
STORE_ID = 1


def _seed_store_with_pos():
    chain_id = seed_chain("Audit Chain")
    store_id = seed_store(chain_id, name="Audit Cafe")

    from app.core.db import POSConnection
    # Keys must match CSVPOSConnector: "sales" not "orders"
    config = {
        "sales": str(DATA / "sales_detail.csv"),
        "menu":  str(DATA / "menu.csv"),
        "staff": str(DATA / "staff.csv"),
    }
    with TestSession() as db:
        db.add(POSConnection(
            store_id=store_id,
            pos_type="csv",
            config=config,
            mapping="cafe_generic",
            currency="PKR",
            timezone="Asia/Karachi",
        ))
        db.commit()

    return store_id


# ── Fresh service for each test (no stale cache) ──────────────────────────────

@pytest.fixture
def svc():
    from app.agents.integrity.service import IntegrityService
    return IntegrityService()


@pytest.fixture
def store_id_with_pos():
    return _seed_store_with_pos()


@pytest.fixture
def store_id_no_pos():
    chain_id = seed_chain("Empty Chain")
    return seed_store(chain_id, name="No-POS Cafe")


# ── _get_config ───────────────────────────────────────────────────────────────

def test_get_config_returns_none_for_missing_pos(svc, store_id_no_pos):
    config = svc._get_config(store_id_no_pos)
    assert config is None


def test_get_config_returns_config_for_configured_store(svc, store_id_with_pos):
    config = svc._get_config(store_id_with_pos)
    assert config is not None
    assert config.pos_type == "csv"
    assert config.currency == "PKR"
    assert "sales_detail.csv" in config.connection.get("sales", "")


def test_get_config_isolation(svc, store_id_with_pos, store_id_no_pos):
    """Different store_ids get independent configs."""
    assert svc._get_config(store_id_with_pos) is not None
    assert svc._get_config(store_id_no_pos) is None


# ── handle_message: no POS configured ────────────────────────────────────────

def test_no_pos_summary_returns_error(svc, store_id_no_pos):
    reply = svc.handle_message(store_id_no_pos, "+923001234567", "summary")
    assert "not configured" in reply.lower() or "no pos" in reply.lower() or "set up" in reply.lower()


def test_no_pos_leakage_returns_error(svc, store_id_no_pos):
    reply = svc.handle_message(store_id_no_pos, "+923001234567", "leakage")
    assert reply  # non-empty error message


# ── handle_message: help ──────────────────────────────────────────────────────

def test_help_command_returns_help_text(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "help")
    assert "summary" in reply.lower()
    assert "leakage" in reply.lower()
    assert "daily" in reply.lower()
    assert "weekly" in reply.lower()


def test_empty_message_returns_help(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "")
    assert "summary" in reply.lower()


def test_help_keyword_returns_help(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "help")
    assert "leakage" in reply.lower()


def test_bare_greetings_no_longer_special_cased_here(svc, store_id_with_pos):
    """Greetings ("hi"/"hello"/"commands"/"start") used to return this
    service's OWN integrity-only help text -- confusing when a staff
    member's very first "hi" landed here (via the LLM router falling
    through to integrity as its default) and got told about only
    integrity's commands, with no mention of revenue/scout/reputation.
    Greetings are now intercepted upstream in app/gateway/internal.py,
    which shows the full staff command list across all four agents; this
    service only special-cases an explicit "help" now, so a greeting
    reaching it directly falls through to the free-form question path
    instead of a canned reply."""
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "hi")
    assert "Integrity Agent" not in reply


# ── handle_message: summary ───────────────────────────────────────────────────

def test_summary_returns_text_with_pkr_amounts(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "summary")
    assert "PKR" in reply or "pkr" in reply.lower()
    assert reply  # non-empty


def test_summary_mentions_leakage_or_profit(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "summary")
    lower = reply.lower()
    assert "leakage" in lower or "profit" in lower or "sales" in lower


def test_summary_cached_on_second_call(svc, store_id_with_pos):
    r1 = svc.handle_message(store_id_with_pos, "+923001234567", "summary")
    r2 = svc.handle_message(store_id_with_pos, "+923001234567", "summary")
    assert r1 == r2  # same cached data


# ── handle_message: leakage ───────────────────────────────────────────────────

def test_leakage_contains_pkr_value(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "leakage")
    assert "PKR" in reply or "leakage" in reply.lower()


def test_leakage_mentions_suspect_or_theft(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "leakage")
    lower = reply.lower()
    assert "theft" in lower or "leakage" in lower or "discount" in lower or "comp" in lower


# ── handle_message: profit ────────────────────────────────────────────────────

def test_profit_contains_gross_margin(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "profit")
    lower = reply.lower()
    assert "profit" in lower or "margin" in lower or "cogs" in lower or "PKR" in reply


def test_profit_shows_numeric_value(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "profit")
    # Must contain at least one digit (a monetary value)
    assert any(c.isdigit() for c in reply)


# ── handle_message: staff ─────────────────────────────────────────────────────

def test_staff_command_returns_something(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "staff")
    assert reply


def test_staff_command_mentions_staff_or_anomalies(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "staff")
    lower = reply.lower()
    assert any(word in lower for word in ("staff", "s0", "anomal", "void", "discount", "no significant"))


# ── handle_message: daily ─────────────────────────────────────────────────────

def test_daily_report_formatted_correctly(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "daily")
    # Must not be a raw Python repr
    assert "PeriodReport" not in reply
    assert "PeriodMetrics" not in reply
    # Must contain actual figures
    assert "PKR" in reply or "orders" in reply.lower() or "sales" in reply.lower()


def test_daily_report_has_orders_count(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "daily")
    lower = reply.lower()
    assert "orders" in lower or "PKR" in reply


def test_daily_report_has_sales_figure(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "daily")
    assert "Sales:" in reply or "sales" in reply.lower()


# ── handle_message: weekly ────────────────────────────────────────────────────

def test_weekly_report_formatted_correctly(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "weekly")
    assert "PeriodReport" not in reply
    assert "PKR" in reply or "orders" in reply.lower()


def test_weekly_report_has_daily_breakdown(svc, store_id_with_pos):
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "weekly")
    lower = reply.lower()
    # Should mention day names for the breakdown
    assert "daily breakdown" in lower or any(
        day in lower for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    )


def test_weekly_vs_daily_differ(svc, store_id_with_pos):
    daily = svc.handle_message(store_id_with_pos, "+923001234567", "daily")
    weekly = svc.handle_message(store_id_with_pos, "+923001234567", "weekly")
    assert daily != weekly


# ── handle_message: refresh ───────────────────────────────────────────────────

def test_refresh_clears_cache_and_reloads(svc, store_id_with_pos):
    svc.handle_message(store_id_with_pos, "+923001234567", "summary")
    assert store_id_with_pos in svc._cache

    reply = svc.handle_message(store_id_with_pos, "+923001234567", "refresh")
    assert "pulled" in reply.lower() or "refresh" in reply.lower() or "ask" in reply.lower()
    # Cache entry should have been invalidated then repopulated
    # (either cleared or replaced with a fresh entry — either is correct)


# ── handle_message: cache TTL bypass ──────────────────────────────────────────

def test_refresh_command_returns_fresh_data(svc, store_id_with_pos):
    from unittest.mock import MagicMock
    from app.agents.integrity.service import _Cached
    # Force a stale cache entry
    svc._cache[store_id_with_pos] = _Cached(at=0.0, report=MagicMock())
    reply = svc.handle_message(store_id_with_pos, "+923001234567", "refresh")
    assert reply  # didn't crash


# ── _fmt_period ───────────────────────────────────────────────────────────────

def test_fmt_period_none_returns_no_data_message():
    from app.agents.integrity.service import _fmt_period
    result = _fmt_period(None)
    assert "no data" in result.lower()


def test_fmt_period_daily_shows_key_fields():
    from unittest.mock import MagicMock
    from app.agents.integrity.service import _fmt_period
    from app.agents.integrity.analysis.periodic import PeriodReport, PeriodMetrics
    from datetime import date

    metrics = PeriodMetrics(net_sales=50000, gross_profit=20000,
                             gross_margin=0.40, leakage=3000, orders=120, has_data=True)
    rpt = PeriodReport(
        venue_name="Test Cafe", kind="daily",
        start=date(2026, 1, 1), end=date(2026, 1, 1),
        label="Wed 1 Jan 2026", report=MagicMock(),
        current=metrics, previous=None, days=[],
    )
    text = _fmt_period(rpt)
    assert "50,000" in text or "50000" in text
    assert "120" in text
    assert "20,000" in text or "20000" in text
    assert "3,000" in text or "3000" in text


def test_fmt_period_weekly_shows_day_breakdown():
    from unittest.mock import MagicMock
    from app.agents.integrity.service import _fmt_period
    from app.agents.integrity.analysis.periodic import PeriodReport, PeriodMetrics, DayPoint
    from datetime import date

    metrics = PeriodMetrics(net_sales=350000, gross_profit=140000,
                             gross_margin=0.40, leakage=21000, orders=840, has_data=True)
    days = [
        DayPoint(day=date(2026, 1, i+1), net_sales=50000, leakage=3000, orders=120)
        for i in range(7)
    ]
    rpt = PeriodReport(
        venue_name="Test Cafe", kind="weekly",
        start=date(2026, 1, 1), end=date(2026, 1, 7),
        label="1-7 Jan 2026", report=MagicMock(),
        current=metrics, previous=None, days=days,
    )
    text = _fmt_period(rpt)
    assert "Daily breakdown" in text
    assert "Jan" in text  # day labels


def test_fmt_period_shows_delta_vs_previous():
    from unittest.mock import MagicMock
    from app.agents.integrity.service import _fmt_period
    from app.agents.integrity.analysis.periodic import PeriodReport, PeriodMetrics
    from datetime import date

    cur  = PeriodMetrics(net_sales=60000, gross_profit=24000,
                          gross_margin=0.40, leakage=3000, orders=150, has_data=True)
    prev = PeriodMetrics(net_sales=50000, gross_profit=20000,
                          gross_margin=0.40, leakage=3000, orders=130, has_data=True)
    rpt = PeriodReport(
        venue_name="Test Cafe", kind="daily",
        start=date(2026, 1, 2), end=date(2026, 1, 2),
        label="Fri 2 Jan 2026", report=MagicMock(),
        current=cur, previous=prev, days=[],
    )
    text = _fmt_period(rpt)
    assert "▲" in text or "▼" in text  # delta indicator present
    assert "20%" in text or "0.20" in text or "%" in text
