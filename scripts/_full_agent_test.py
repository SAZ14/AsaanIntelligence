"""Comprehensive agent end-to-end test with fake data injection."""
import sys, time, json, hashlib
sys.path.insert(0, ".")
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(".env")

OK   = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
results = []

def test(label, fn, skip=False):
    if skip:
        results.append((SKIP, label, "skipped"))
        print(f"{SKIP} {label}")
        return
    t0 = time.monotonic()
    try:
        result = fn()
        elapsed = time.monotonic() - t0
        preview = str(result)[:180].replace("\n", " ")
        results.append((OK, label, preview))
        print(f"{OK} {label} ({elapsed:.1f}s)")
        print(f"     {preview}")
    except Exception as e:
        import traceback
        elapsed = time.monotonic() - t0
        results.append((FAIL, label, str(e)))
        tb = traceback.format_exc().strip().split("\n")
        print(f"{FAIL} {label} ({elapsed:.1f}s)")
        print(f"     ERROR: {e}")
        print(f"     {tb[-1]}")
    print()

STORE_SR   = 1   # Sugar Rush — CSV files in data/, integrity configured
STORE_AN   = 5   # Anatummy  — reputation config, 23 competitors
STAFF_PHONE = "+923001234567"

print("=" * 65)
print("COMPREHENSIVE AGENT END-TO-END TESTS")
print("=" * 65)
print()

# ─────────────────────────────────────────────────────────────────────
print("━━━ 1. INTEGRITY — Sugar Rush (pos=csv, real data) ━━━\n")

from app.agents.integrity.service import get_service
svc = get_service()

test("integrity: help",    lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "help"))
test("integrity: summary", lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "summary"))
test("integrity: leakage", lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "leakage"))
test("integrity: profit",  lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "profit"))
test("integrity: staff",   lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "staff"))
test("integrity: daily",   lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "daily"))
test("integrity: weekly",  lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "weekly"))
test("integrity: NL — voids",   lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "how much did we lose to voids?"))
test("integrity: NL — margin",  lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "what is our gross margin?"))
test("integrity: NL — who had most comps?", lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "which staff member had the most comps?"))
test("integrity: refresh", lambda: svc.handle_message(STORE_SR, STAFF_PHONE, "refresh"))

# ─────────────────────────────────────────────────────────────────────
print("━━━ 2. INTEGRITY — Anatummy (whatsapp_csv, fake data) ━━━\n")

FAKE_SALES = """order_id,datetime,staff_id,staff_name,item_sku,item_name,category,qty,unit_price,line_amount,discount_amount,is_void,void_after_fire,is_comp,order_status,payment_method,payment_amount,tax_rate
ORD001,2026-06-01 13:42:00,S03,Ali,MN12,Smash Burger,Main,2,850,1700,0,0,0,0,closed,cash,1700,0
ORD002,2026-06-01 14:10:00,S07,Sara,MN12,Smash Burger,Main,1,850,850,200,0,0,1,closed,card,650,0
ORD003,2026-06-01 14:30:00,S03,Ali,DR05,Coke,Drink,3,150,450,0,0,0,0,closed,cash,450,0
ORD004,2026-06-01 15:00:00,S03,Ali,MN15,Loaded Fries,Side,2,400,800,0,1,1,0,void,cash,0,0
ORD005,2026-06-02 12:00:00,S07,Sara,MN12,Smash Burger,Main,4,850,3400,0,0,0,0,closed,card,3400,0
ORD006,2026-06-02 12:30:00,S07,Sara,MN15,Loaded Fries,Side,2,400,800,400,0,0,0,closed,card,400,0
ORD007,2026-06-03 13:00:00,S01,Ahmed,MN12,Smash Burger,Main,1,850,850,0,0,0,0,closed,cash,850,0
ORD008,2026-06-03 13:30:00,S01,Ahmed,DR05,Coke,Drink,2,150,300,0,0,0,0,closed,cash,300,0"""

FAKE_MENU = """sku,name,category,cost,price
MN12,Smash Burger,Main,350,850
MN15,Loaded Fries,Side,120,400
DR05,Coke,Drink,40,150"""

FAKE_STAFF = """staff_id,name,role
S01,Ahmed,Manager
S03,Ali,Cashier
S07,Sara,Cashier"""

from app.core.db import SessionLocal, UploadedFile, POSConnection
import datetime

def inject_csv_uploads():
    with SessionLocal() as db:
        for ftype, content in [
            ("pos_sales", FAKE_SALES),
            ("pos_menu",  FAKE_MENU),
            ("pos_staff", FAKE_STAFF),
        ]:
            row = db.query(UploadedFile).filter(
                UploadedFile.store_id == STORE_AN,
                UploadedFile.file_type == ftype,
            ).first()
            if row:
                row.content = content
                row.uploaded_at = datetime.datetime.utcnow()
            else:
                db.add(UploadedFile(
                    store_id=STORE_AN, file_type=ftype,
                    filename=f"test_{ftype}.csv",
                    content=content, uploaded_by=STAFF_PHONE,
                ))
        pos = db.query(POSConnection).filter(POSConnection.store_id == STORE_AN).first()
        if not pos:
            db.add(POSConnection(
                store_id=STORE_AN, pos_type="whatsapp_csv",
                config={}, mapping="cafe_generic",
                currency="PKR", timezone="Asia/Karachi",
            ))
        elif pos.pos_type != "whatsapp_csv":
            pos.pos_type = "whatsapp_csv"
        db.commit()
    return "Fake CSV data injected for Anatummy (store 5)"

test("setup: inject fake CSV for Anatummy", inject_csv_uploads)

# Force integrity service to pick up whatsapp_csv connector
import app.agents.integrity.service as _isvc
_isvc._service = None

svc2 = get_service()
test("integrity(csv-upload): summary",  lambda: svc2.handle_message(STORE_AN, STAFF_PHONE, "summary"))
test("integrity(csv-upload): leakage",  lambda: svc2.handle_message(STORE_AN, STAFF_PHONE, "leakage"))
test("integrity(csv-upload): profit",   lambda: svc2.handle_message(STORE_AN, STAFF_PHONE, "profit"))
test("integrity(csv-upload): staff",    lambda: svc2.handle_message(STORE_AN, STAFF_PHONE, "staff"))
test("integrity(csv-upload): daily",    lambda: svc2.handle_message(STORE_AN, STAFF_PHONE, "daily"))

# ─────────────────────────────────────────────────────────────────────
print("━━━ 3. REPUTATION AGENT ━━━\n")

from app.agents.reputation import process_reputation_owner_reply
from app.core.db import Finding, ScoutRun

def inject_pending_review(hash_suffix, text, rating, label=""):
    fake_hash = hashlib.md5(f"test_review_anatummy_{hash_suffix}".encode()).hexdigest()
    with SessionLocal() as db:
        run = db.query(ScoutRun).filter(ScoutRun.store_id == STORE_AN).first()
        if not run:
            run = ScoutRun(store_id=STORE_AN, command="check", status="completed")
            db.add(run); db.flush()
        run_id = run.id
        db.query(Finding).filter(
            Finding.store_id == STORE_AN,
            Finding.content_hash == fake_hash,
        ).delete()
        db.add(Finding(
            store_id=STORE_AN, run_id=run_id,
            competitor_name="Anatummy",
            source_platform="Google Maps", update_type="review",
            content_text=text, rating=rating,
            content_hash=fake_hash,
            ai_summary=json.dumps({
                "sentiment": "negative" if rating <= 2 else "neutral",
                "rating": rating,
                "topics": ["service", "food"],
                "draft_reply": "Thank you for your feedback. We are working on it!",
                "status": "pending",
            }),
            relevance_score=0,
        ))
        db.commit()
    return f"Injected {label or text[:40]!r} (rating={rating})"

test("setup: inject pending 2-star review", lambda: inject_pending_review(
    "001",
    "Very slow service, food was cold. The smash burger was decent but the experience was bad.",
    2, "2-star review"
))

test("reputation: chat — what are people saying?", lambda: process_reputation_owner_reply(
    STAFF_PHONE, "what are people saying about us?", store_id=STORE_AN
))
test("reputation: edit (rewrite draft reply)", lambda: process_reputation_owner_reply(
    STAFF_PHONE,
    "edit Thank you for visiting! We have addressed the kitchen speed and temperature issues. DM us for a complimentary meal on your next visit.",
    store_id=STORE_AN,
))
test("reputation: post (publish reply)", lambda: process_reputation_owner_reply(
    STAFF_PHONE, "post", store_id=STORE_AN
))
test("reputation: post again — should say none pending", lambda: process_reputation_owner_reply(
    STAFF_PHONE, "post", store_id=STORE_AN
))

test("setup: inject second pending review (1-star)", lambda: inject_pending_review(
    "002",
    "Absolutely terrible. Cold burger, rude staff. Will not return.",
    1, "1-star review"
))
test("reputation: ignore (dismiss review)", lambda: process_reputation_owner_reply(
    STAFF_PHONE, "ignore", store_id=STORE_AN
))
test("reputation: ignore again — should say none", lambda: process_reputation_owner_reply(
    STAFF_PHONE, "ignore", store_id=STORE_AN
))

test("setup: inject 3-star review for chat test", lambda: inject_pending_review(
    "003",
    "Decent burgers. Service was acceptable. Fries were soggy. Overall 3 stars.",
    3, "3-star review"
))
test("reputation: chat — any patterns in complaints?", lambda: process_reputation_owner_reply(
    STAFF_PHONE, "any patterns in what customers are complaining about?", store_id=STORE_AN
))

# ─────────────────────────────────────────────────────────────────────
print("━━━ 4. REVENUE AGENT ━━━\n")

from app.core.db import RevenueConnection

def setup_revenue():
    with SessionLocal() as db:
        existing = db.query(RevenueConnection).filter(RevenueConnection.store_id == STORE_SR).first()
        if not existing:
            db.add(RevenueConnection(
                store_id=STORE_SR,
                data_dir="D:\\Codes\\sugarrush\\data",
                db_path=":memory:",
                config={},
            ))
            db.commit()
            return "Revenue connection created for Sugar Rush"
        return "Revenue connection already exists"

test("setup: create revenue connection for Sugar Rush", setup_revenue)

import app.agents.revenue.registry as _rreg
_rreg._registry = None
from app.agents.revenue.registry import get_registry
registry = get_registry()

test("revenue: help / general query",        lambda: registry.handle(STORE_SR, STAFF_PHONE, "revenue").text)
test("revenue: how are sales looking?",      lambda: registry.handle(STORE_SR, STAFF_PHONE, "how are sales looking?").text)
test("revenue: what is selling best?",       lambda: registry.handle(STORE_SR, STAFF_PHONE, "what is selling best this week?").text)
test("revenue: pricing recommendations",     lambda: registry.handle(STORE_SR, STAFF_PHONE, "any pricing recommendations?").text)
test("revenue: weekly sales trend",          lambda: registry.handle(STORE_SR, STAFF_PHONE, "weekly sales trend").text)
test("revenue: upsell strategy",             lambda: registry.handle(STORE_SR, STAFF_PHONE, "what should we push to increase basket size?").text)
test("revenue: unconfigured store (store 5 has no connection)", lambda: registry.handle(STORE_AN, STAFF_PHONE, "revenue").text)

# ─────────────────────────────────────────────────────────────────────
print("━━━ 5. CUSTOMER LOYALTY AGENT ━━━\n")

from app.gateway.customer import handle_customer_for_store
from app.core.db import VenueConfig, CommunityMember

CUST1 = "+923777000011"
CUST2 = "+923777000022"

def ensure_venue():
    with SessionLocal() as db:
        vc = db.query(VenueConfig).filter(VenueConfig.store_id == STORE_SR).first()
        if not vc:
            db.add(VenueConfig(
                store_id=STORE_SR, venue_name="Sugar Rush",
                stamp_goal=5, reward_text="1 Free Sundae",
                winback_days=7, code_expiry_days=30,
                owner_phones=[STAFF_PHONE],
            ))
            db.commit()
        return f"VenueConfig OK (goal={vc.stamp_goal if vc else 5})"

def clear_test_customers():
    with SessionLocal() as db:
        db.query(CommunityMember).filter(
            CommunityMember.store_id == STORE_SR,
            CommunityMember.phone.in_([CUST1, CUST2]),
        ).delete()
        db.commit()
    return "Test customers cleared"

test("setup: ensure venue config",    ensure_venue)
test("setup: clear test customers",   clear_test_customers)

test("customer: first message — greeting", lambda: handle_customer_for_store(CUST1, "hi", STORE_SR))
test("customer: provide name (Zara)",      lambda: handle_customer_for_store(CUST1, "Zara", STORE_SR))
test("customer: check stamps (0)",         lambda: handle_customer_for_store(CUST1, "how many stamps do I have?", STORE_SR))
test("customer: ask about reward",         lambda: handle_customer_for_store(CUST1, "what do I get when I complete the card?", STORE_SR))

from app.agents.customer.community.stamps import apply_stamp
from app.agents.customer.community.store import (
    load_members, save_members, load_venue_config,
)
from app.agents.customer.community.models import VenueConfig as CustVenueConfig

def give_n_stamps(phone, n):
    msgs = []
    for i in range(n):
        members = load_members(STORE_SR)
        cfg = load_venue_config(STORE_SR)
        member = members.get(phone)
        if not member:
            return f"member not found for {phone}"
        result = apply_stamp(member, f"STAFF_STAMP_{i}", cfg, STORE_SR)
        save_members(STORE_SR, {phone: member})
        msgs.append(result.message[:40] if result else "?")
    return " | ".join(msgs[-2:])

test("customer: give 4 stamps",         lambda: give_n_stamps(CUST1, 4))
test("customer: check stamps (4 of 5)", lambda: handle_customer_for_store(CUST1, "stamps", STORE_SR))
test("customer: give 1 more (triggers reward)", lambda: give_n_stamps(CUST1, 1))
test("customer: check after reward unlocked",   lambda: handle_customer_for_store(CUST1, "stamps", STORE_SR))
test("customer: redeem reward",                 lambda: handle_customer_for_store(CUST1, "redeem", STORE_SR))

# Second customer flow
test("customer2: greeting (Faraz)",      lambda: handle_customer_for_store(CUST2, "hello", STORE_SR))
test("customer2: provide name",          lambda: handle_customer_for_store(CUST2, "Faraz", STORE_SR))
test("customer2: stamps query",          lambda: handle_customer_for_store(CUST2, "how many do I need for a reward?", STORE_SR))

# ─────────────────────────────────────────────────────────────────────
print("━━━ 6. SCOUT AGENT ━━━\n")

from app.agents.scout.pipeline import run as scout_run
from app.agents.scout.analysis import classify_intent

test("scout: help", lambda: scout_run("help", store_id=STORE_AN))

intent_cases = [
    ("what is TBC Burger doing on Instagram?",       "instagram"),
    ("any new menu items from competitors recently?", "new_products"),
    ("how are competitors pricing their burgers?",    "pricing"),
    ("any new branches opening nearby?",              "branch_updates"),
    ("what market gaps can we exploit?",              "opportunities"),
    ("what are customers saying about rival burgers?","reviews"),
]

def test_intents():
    lines = []
    for msg, exp_key in intent_cases:
        got = classify_intent(msg, "Anatummy", "burgers")
        lines.append(f"  \"{msg[:50]}\" => {got}")
    return "\n".join(lines)

test("scout: 6 intent classifications", test_intents)

# ─────────────────────────────────────────────────────────────────────
print("━━━ 7. LLM ROUTING + RESPONSE ADAPTATION ━━━\n")

from app.gateway.internal import _classify_with_llm, _adapt_response

routing_cases = [
    ("show me the leakage report",                    "integrity", "leakage"),
    ("who had the worst void rate this week?",        "integrity", "staff"),
    ("what is our gross margin?",                     "integrity", "profit"),
    ("give me the daily breakdown",                   "integrity", "daily"),
    ("how is revenue trending?",                      "revenue",   "general"),
    ("what are customers saying on Google?",          "reputation", "chat"),
    ("run a competitor scan now",                     "scout",      "scout"),
]

def test_routing():
    lines = []
    for msg, exp_agent, exp_cmd in routing_cases:
        agent, cmd = _classify_with_llm(msg)
        ok = agent == exp_agent
        lines.append(f"{OK if ok else FAIL} \"{msg[:48]}\" -> {agent}/{cmd} (want {exp_agent})")
    return "\n".join(lines)

test("llm_routing: 7 message classifications", test_routing)

def test_adapt():
    raw = (
        "Net sales PKR 1,200,000. Leakage PKR 45,000 (3.75%). "
        "Top offender: Ali — 12 voids, 3 post-fire cancels."
    )
    return _adapt_response("how bad is our leakage situation?", raw)

test("llm_routing: response adaptation", test_adapt)

# ─────────────────────────────────────────────────────────────────────
print("━━━ SUMMARY ━━━\n")
passed  = sum(1 for s,_,_ in results if s == OK)
failed  = sum(1 for s,_,_ in results if s == FAIL)
skipped = sum(1 for s,_,_ in results if s == SKIP)
total   = len(results)
print(f"  {passed}/{total} passed  |  {failed} failed  |  {skipped} skipped")

if failed:
    print("\nFAILURES:")
    for s, label, msg in results:
        if s == FAIL:
            print(f"  [{label}]  {msg[:120]}")
print()
