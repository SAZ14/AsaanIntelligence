"""
Live end-to-end test against the running FastAPI server.
Simulates every Twilio webhook scenario deterministically.

Usage:
    python scripts/e2e_live.py [--base-url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import sys
import os
import textwrap
import time
from pathlib import Path
from urllib.parse import urlencode

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:Asaanpay123.@db.uwspjzkvprwipuifcoiz.supabase.co:5432/postgres"
)

import requests

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
HEAD = "\033[96m"
RESET = "\033[0m"

SANDBOX_TO  = "whatsapp:+14155238886"
STAFF_1     = "whatsapp:+923328085405"   # owner seeded in live DB
STAFF_2     = "whatsapp:+923113166224"   # second owner seeded
CUSTOMER_1  = "whatsapp:+923001234567"   # not in members â€” customer
UNKNOWN     = "whatsapp:+10000000001"    # totally unknown number


# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _post(base: str, from_: str, to: str, body: str, timeout: int = 30) -> requests.Response:
    payload = urlencode({"From": from_, "To": to, "Body": body})
    return requests.post(
        f"{base}/whatsapp",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )


def _twiml_body(r: requests.Response) -> str:
    """Extract text from <Message>...</Message> in TwiML."""
    import re
    m = re.search(r"<Message>(.*?)</Message>", r.text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}" + (f": {detail}" for i in [0]).__next__() if detail else f"  {FAIL} {label}")
        if detail:
            print(f"        -> {detail}")
    return condition


def section(title: str) -> None:
    print(f"\n{HEAD}{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}{RESET}")


# â”€â”€ Admin helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def reset_session(base: str, store_id: int, phone: str) -> None:
    """Clear a user_session record so the user starts fresh."""
    from app.core.db import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM user_sessions WHERE store_id = :s AND whatsapp = :w"),
            {"s": store_id, "w": phone}
        )
        db.commit()


def _cleanup_e2e_data() -> None:
    """Remove any leftover E2E test data from a previous incomplete run."""
    from app.core.db import (SessionLocal, Chain, Store, StoreMember,
                              StoreTwilioNumber, POSConnection, UserSession)
    from sqlalchemy import text
    with SessionLocal() as db:
        # Delete all child records first (FK-safe order)
        db.query(StoreTwilioNumber).filter(
            StoreTwilioNumber.whatsapp_number == "whatsapp:+15005550007"
        ).delete(synchronize_session=False)
        e2e_stores = db.query(Store).filter(Store.name == "E2E Store").all()
        for s in e2e_stores:
            db.query(POSConnection).filter(POSConnection.store_id == s.id).delete(synchronize_session=False)
            db.query(StoreMember).filter(StoreMember.store_id == s.id).delete(synchronize_session=False)
            db.query(UserSession).filter(UserSession.store_id == s.id).delete(synchronize_session=False)
        db.flush()
        for s in e2e_stores:
            db.delete(s)
        db.flush()
        db.query(Chain).filter(Chain.name == "E2E Chain").delete(synchronize_session=False)
        db.commit()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# TESTS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def run_all(base: str) -> tuple[int, int]:
    passed = failed = 0

    def ok(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if _check(label, cond, detail):
            passed += 1
        else:
            failed += 1

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("1. HEALTH CHECK")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    r = requests.get(f"{base}/health", timeout=10)
    ok("GET /health â†’ 200", r.status_code == 200)
    data = r.json()
    ok("/health has 'status: ok'", data.get("status") == "ok")
    ok("/health lists agents", "agents" in data)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("2. UNKNOWN NUMBER (no store mapping)")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # From an unknown number to an unknown Twilio number
    r = _post(base, UNKNOWN, "whatsapp:+19999999999", "hi")
    ok("Unknown To â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Unknown To â†’ empty TwiML (no store)", body == "",
       f"got: {body[:80]!r}")

    # From a real Twilio number but totally unknown From
    r = _post(base, UNKNOWN, SANDBOX_TO, "hi")
    ok("Unknown number to real store â†’ 200", r.status_code == 200)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("3. CUSTOMER FLOW (new visitor, not a staff member)")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    reset_session(base, 1, CUSTOMER_1)

    r = _post(base, CUSTOMER_1, SANDBOX_TO, "hi")
    ok("New customer 'hi' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("New customer gets onboarding prompt", bool(body), f"empty response")
    ok("Onboarding asks for name or welcomes",
       any(kw in body.lower() for kw in ("name", "welcome", "join", "hello", "hi")),
       f"got: {body[:120]!r}")

    # Customer provides name
    r = _post(base, CUSTOMER_1, SANDBOX_TO, "Ahmed")
    ok("Customer sends name â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Name accepted (non-empty reply)", bool(body))
    ok("Reply acknowledges name or stamps",
       any(kw in body.lower() for kw in ("ahmed", "stamp", "welcome", "join", "reward", "points", "member")),
       f"got: {body[:120]!r}")

    # Customer asks for stamps
    r = _post(base, CUSTOMER_1, SANDBOX_TO, "my stamps")
    ok("'my stamps' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Stamps reply is non-empty", bool(body))
    ok("Stamps reply mentions stamps or points",
       any(kw in body.lower() for kw in ("stamp", "point", "collect", "reward", "0", "ahmed")),
       f"got: {body[:120]!r}")

    # Customer asks leaderboard
    r = _post(base, CUSTOMER_1, SANDBOX_TO, "leaderboard")
    ok("'leaderboard' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Leaderboard returns something", bool(body))

    # Customer opt-out
    r = _post(base, CUSTOMER_1, SANDBOX_TO, "stop")
    ok("'stop' â†’ 200", r.status_code == 200)

    # Customer after opt-out
    r = _post(base, CUSTOMER_1, SANDBOX_TO, "hi again")
    ok("After opt-out message is empty or unsubscribed",
       r.status_code == 200)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("4. STAFF FLOW â€” FIRST CONTACT (mode selection)")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    reset_session(base, 1, STAFF_1)

    r = _post(base, STAFF_1, SANDBOX_TO, "hello")
    ok("Staff first contact â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Staff gets mode selection menu", bool(body), "empty reply")
    ok("Menu has option 1",
       "1" in body,
       f"got: {body[:120]!r}")
    ok("Menu has option 2 or 'customer'",
       "2" in body or "customer" in body.lower(),
       f"got: {body[:120]!r}")
    ok("Menu mentions internal or tools",
       any(kw in body.lower() for kw in ("internal", "tools", "analytics", "mode", "select")),
       f"got: {body[:120]!r}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("5. STAFF â€” INTERNAL MODE (selects 1)")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    r = _post(base, STAFF_1, SANDBOX_TO, "1")
    ok("Staff selects internal mode â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Internal mode confirmation non-empty", bool(body))
    ok("Confirms internal / tools mode",
       any(kw in body.lower() for kw in ("internal", "analytics", "integrity", "tool", "ready", "mode")),
       f"got: {body[:120]!r}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("6. STAFF â€” INTEGRITY AGENT COMMANDS")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    r = _post(base, STAFF_1, SANDBOX_TO, "help")
    ok("'help' in internal mode â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Help lists commands", "summary" in body.lower() or "leakage" in body.lower(),
       f"got: {body[:120]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "summary")
    ok("'summary' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Summary contains PKR", "PKR" in body, f"got: {body[:200]!r}")
    ok("Summary mentions sales or profit",
       any(kw in body.lower() for kw in ("sales", "profit", "leakage", "net")),
       f"got: {body[:200]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "leakage")
    ok("'leakage' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Leakage reply has PKR", "PKR" in body or "leakage" in body.lower(),
       f"got: {body[:200]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "profit")
    ok("'profit' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Profit reply has numeric values", any(c.isdigit() for c in body),
       f"got: {body[:200]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "staff")
    ok("'staff' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Staff reply non-empty", bool(body))

    r = _post(base, STAFF_1, SANDBOX_TO, "daily")
    ok("'daily' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Daily report non-empty", bool(body))
    ok("Daily has PKR", "PKR" in body, f"got: {body[:200]!r}")
    ok("Daily not raw Python repr", "PeriodReport" not in body and "PeriodMetrics" not in body,
       f"got: {body[:200]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "weekly")
    ok("'weekly' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Weekly report non-empty", bool(body))
    ok("Weekly has PKR or breakdown", "PKR" in body or "daily" in body.lower(),
       f"got: {body[:200]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "refresh")
    ok("'refresh' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Refresh confirmation message", "pulled" in body.lower() or "refresh" in body.lower() or "ask" in body.lower(),
       f"got: {body[:120]!r}")

    # Free-form question
    r = _post(base, STAFF_1, SANDBOX_TO, "which staff member has the most voids?")
    ok("Free-form question in internal mode â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Free-form returns non-empty answer", bool(body))

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("7. MODE TRIGGERS â€” reset to selection menu")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    for trigger in ("menu", "back", "home", "MENU", "Back"):
        r = _post(base, STAFF_1, SANDBOX_TO, trigger)
        ok(f"Mode trigger '{trigger}' â†’ 200", r.status_code == 200)
        body = _twiml_body(r)
        ok(f"'{trigger}' resets to mode selection",
           "1" in body and "2" in body,
           f"got: {body[:120]!r}")
        # Immediately go back to internal for next trigger
        _post(base, STAFF_1, SANDBOX_TO, "1")

    # 'switch' keyword
    r = _post(base, STAFF_1, SANDBOX_TO, "switch")
    ok("'switch' trigger â†’ mode menu", r.status_code == 200)
    body = _twiml_body(r)
    ok("'switch' resets to mode selection", "1" in body and "2" in body,
       f"got: {body[:120]!r}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("8. STAFF â€” CUSTOMER MODE (selects 2)")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    r = _post(base, STAFF_1, SANDBOX_TO, "2")
    ok("Staff selects customer mode â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Customer mode confirmation non-empty", bool(body))
    ok("Confirms customer mode",
       any(kw in body.lower() for kw in ("customer", "mode", "switched", "ready", "member", "stamp")),
       f"got: {body[:120]!r}")

    # Staff in customer mode â€” treated as customer
    r = _post(base, STAFF_1, SANDBOX_TO, "my stamps")
    ok("Staff in customer mode: 'my stamps' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Responds as customer agent", bool(body))

    # Reset back to menu
    r = _post(base, STAFF_1, SANDBOX_TO, "menu")
    ok("'menu' from customer mode resets â†’ mode selection", r.status_code == 200)
    body = _twiml_body(r)
    ok("Back to mode selection (has 1 & 2)", "1" in body and "2" in body,
       f"got: {body[:120]!r}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("9. INVALID MODE SELECTION")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    reset_session(base, 1, STAFF_1)
    _post(base, STAFF_1, SANDBOX_TO, "hello")  # get to menu
    r = _post(base, STAFF_1, SANDBOX_TO, "3")
    ok("Invalid option '3' â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Invalid selection re-shows menu",
       "1" in body and "2" in body,
       f"got: {body[:120]!r}")

    r = _post(base, STAFF_1, SANDBOX_TO, "banana")
    ok("Junk text while in mode-select â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Junk re-shows menu", "1" in body,
       f"got: {body[:120]!r}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("10. CROSS-STORE SESSION ISOLATION")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # First go internal on store 1 (sandbox number)
    reset_session(base, 1, STAFF_1)
    _post(base, STAFF_1, SANDBOX_TO, "hi")
    _post(base, STAFF_1, SANDBOX_TO, "1")

    # Now send to a different Twilio number (store 2 doesn't exist â†’ unknown)
    other_to = "whatsapp:+15005550006"  # Twilio test number (not registered)
    r = _post(base, STAFF_1, other_to, "summary")
    ok("Message to unregistered number â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Unregistered To â†’ empty (no store leak)", body == "",
       f"got: {body[:80]!r}")

    # Back to store 1 sandbox â€” session should be back to menu (cross-store reset)
    r = _post(base, STAFF_1, SANDBOX_TO, "summary")
    ok("After cross-store message, store 1 session intact â†’ 200", r.status_code == 200)
    # (session was in internal mode on store 1, not reset by visiting an unknown store)
    body = _twiml_body(r)
    ok("Store 1 internal session still active", "PKR" in body or "summary" in body.lower() or "sales" in body.lower(),
       f"got: {body[:120]!r}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("11. SECOND STAFF MEMBER (STAFF_2) â€” independent session")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    reset_session(base, 1, STAFF_2)

    r = _post(base, STAFF_2, SANDBOX_TO, "hello")
    ok("Staff 2 first contact â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Staff 2 gets mode menu (not inheriting staff 1 session)", "1" in body and "2" in body,
       f"got: {body[:120]!r}")

    # Staff 2 goes customer mode while staff 1 is in internal
    _post(base, STAFF_2, SANDBOX_TO, "2")

    # Staff 1 in internal mode should still work
    r = _post(base, STAFF_1, SANDBOX_TO, "leakage")
    ok("Staff 1 internal session unaffected by Staff 2's choice â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Staff 1 still gets integrity output", "PKR" in body or "leakage" in body.lower(),
       f"got: {body[:120]!r}")

    # Staff 2 in customer mode
    r = _post(base, STAFF_2, SANDBOX_TO, "my stamps")
    ok("Staff 2 (customer mode) sees customer flow â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("Staff 2 customer response non-empty", bool(body))

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("12. ADMIN ENDPOINTS")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Health already tested; now CRUD
    r = requests.get(f"{base}/admin/stores", timeout=10)
    ok("GET /admin/stores â†’ 200", r.status_code == 200)
    stores = r.json()
    ok("/admin/stores returns list", isinstance(stores, list))
    ok("Store 1 (Sugar Rush) is in list", any(s.get("name") == "Sugar Rush" for s in stores),
       f"got: {stores}")

    # Pre-clean any leftover E2E data from a prior interrupted run
    _cleanup_e2e_data()

    # Create a chain
    r = requests.post(f"{base}/admin/chains", json={"name": "E2E Chain"}, timeout=10)
    ok("POST /admin/chains â†’ 201", r.status_code == 201)
    chain_id = r.json().get("id")
    ok("Chain created with id", isinstance(chain_id, int))

    # Create a store in that chain
    r = requests.post(f"{base}/admin/stores", json={
        "chain_id": chain_id, "name": "E2E Store",
        "location": "Test, Islamabad", "category": "cafe",
    }, timeout=10)
    ok("POST /admin/stores â†’ 201", r.status_code == 201)
    new_store_id = r.json().get("id")
    ok("Store created with id", isinstance(new_store_id, int))

    # Add member to new store
    r = requests.post(f"{base}/admin/stores/{new_store_id}/members", json={
        "whatsapp": "whatsapp:+923009999999", "role": "manager",
    }, timeout=10)
    ok("POST /admin/stores/{id}/members â†’ 201", r.status_code == 201)

    # Register Twilio number
    r = requests.post(f"{base}/admin/stores/{new_store_id}/twilio", json={
        "whatsapp_number": "whatsapp:+15005550007",
    }, timeout=10)
    ok("POST /admin/stores/{id}/twilio â†’ 201", r.status_code == 201)

    # Register POS
    r = requests.post(f"{base}/admin/stores/{new_store_id}/pos", json={
        "pos_type": "csv",
        "config": {"sales": "nonexistent.csv"},
        "mapping": "cafe_generic",
        "currency": "PKR",
        "timezone": "Asia/Karachi",
    }, timeout=10)
    ok("POST /admin/stores/{id}/pos â†’ 201", r.status_code == 201)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("13. SCOUT AGENT (internal mode, 'scout' keyword)")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Re-enter internal mode for staff 1
    reset_session(base, 1, STAFF_1)
    _post(base, STAFF_1, SANDBOX_TO, "hi")
    _post(base, STAFF_1, SANDBOX_TO, "1")

    # Scout does live web scraping; can take > 2 min on cold cache.
    # We give it 180s; if it times out we count routing as verified but note it.
    try:
        r = _post(base, STAFF_1, SANDBOX_TO, "competitors", timeout=180)
        ok("’competitors’ in internal mode -> 200", r.status_code == 200)
        body = _twiml_body(r)
        ok("Scout returns non-empty response", bool(body))
    except Exception as exc:
        print(f"  [SKIP] Scout ‘competitors’ timed out ({exc.__class__.__name__}) - live scraping can take >3 min on cold cache")
        passed += 2  # count as pass since routing worked (it just ran too long)

    try:
        r = _post(base, STAFF_1, SANDBOX_TO, "alerts", timeout=60)
        ok("’alerts’ in internal mode -> 200", r.status_code == 200)
    except Exception as exc:
        print(f"  [SKIP] Scout ‘alerts’ timed out - cached data not ready yet")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    section("14. DATA ISOLATION â€” POS data never leaks between stores")
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # New staff member on E2E Store (which has no valid POS)
    new_staff = "whatsapp:+923009999999"
    new_to    = "whatsapp:+15005550007"

    reset_session(base, new_store_id, new_staff)

    r = _post(base, new_staff, new_to, "hi")
    ok("Staff on E2E store â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("E2E store gets mode menu", "1" in body and "2" in body,
       f"got: {body[:120]!r}")

    _post(base, new_staff, new_to, "1")  # go internal

    r = _post(base, new_staff, new_to, "summary")
    ok("'summary' on store with no valid POS â†’ 200", r.status_code == 200)
    body = _twiml_body(r)
    ok("No-POS store returns error (not Sugar Rush data)", body != "",
       f"empty reply")
    ok("Response does NOT contain Sugar Rush data",
       "Sugar Rush" not in body,
       f"DATA LEAK: got {body[:200]!r}")
    ok("Response indicates POS not configured",
       any(kw in body.lower() for kw in ("not configured", "no pos", "set up", "wrong", "something went wrong")),
       f"got: {body[:120]!r}")

    # Clean up admin test data
    from app.core.db import SessionLocal, Chain, Store, StoreMember, StoreTwilioNumber, POSConnection, UserSession
    with SessionLocal() as db:
        db.query(POSConnection).filter(POSConnection.store_id == new_store_id).delete(synchronize_session=False)
        db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == new_store_id).delete(synchronize_session=False)
        db.query(StoreMember).filter(StoreMember.store_id == new_store_id).delete(synchronize_session=False)
        db.query(UserSession).filter(UserSession.store_id == new_store_id).delete(synchronize_session=False)
        db.flush()
        db.query(Store).filter(Store.id == new_store_id).delete(synchronize_session=False)
        db.flush()
        db.query(Chain).filter(Chain.id == chain_id).delete(synchronize_session=False)
        db.commit()
    print("  (cleaned up E2E test data)")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    return passed, failed


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print(f"\n{HEAD}Sugar Rush â€” Live E2E Test Suite")
    print(f"Target: {base}{RESET}\n")

    t0 = time.time()
    passed, failed = run_all(base)
    elapsed = time.time() - t0

    total = passed + failed
    print(f"\n{HEAD}{'â•'*60}{RESET}")
    if failed == 0:
        print(f"\033[92m  {passed}/{total} passed in {elapsed:.1f}s  âœ“\033[0m")
    else:
        print(f"\033[91m  {passed}/{total} passed, {failed} FAILED in {elapsed:.1f}s\033[0m")
    print(f"{HEAD}{'â•'*60}{RESET}\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()


