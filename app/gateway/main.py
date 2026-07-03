"""Central server — single FastAPI app, single WhatsApp webhook.

One Twilio number per restaurant branch. The server checks whether the
sender is a whitelisted staff member of that branch:

  • Staff   → mode-selection screen (1 = internal tools, 2 = customer app)
             — or whichever mode they're already in.
  • Customer → straight to the loyalty / community agent.

Staff can type "menu" at any time to return to the mode-selection screen.
Within internal mode, messages are routed transparently to scout, integrity,
or revenue based on keyword — no agent-selection needed from the user.

Webhooks:
  POST /whatsapp          ← Twilio (form-encoded, TwiML response)
  POST /openwa/webhook    ← OpenWA (JSON, 200 OK + outbound API replies)

Admin endpoints (no auth — add middleware before production):
  POST   /admin/chains
  POST   /admin/stores
  POST   /admin/stores/{id}/members
  DELETE /admin/stores/{id}/members
  POST   /admin/stores/{id}/locations
  POST   /admin/stores/{id}/pos
  POST   /admin/stores/{id}/revenue
  POST   /admin/stores/{id}/customer
  POST   /admin/stores/{id}/twilio
  POST   /admin/stores/{id}/openwa
  DELETE /admin/stores/{id}/openwa
  GET    /admin/stores
  GET    /admin/stores/{id}
  GET    /health
  GET    /report/{store_id}.pdf
"""
from __future__ import annotations

import logging
import os
import re as _re
import time as _time
from collections import defaultdict
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse
import json as _json_mod

logger = logging.getLogger(__name__)


async def _parse_body(request: Request) -> dict:
    """Accept JSON or form-encoded body for admin endpoints."""
    ct = request.headers.get("content-type", "")
    raw = await request.body()
    if not raw:
        return {}
    if "application/json" in ct:
        return _json_mod.loads(raw)
    return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

# Values stored in user_sessions.active_agent to track mode
MODE_INTERNAL = "internal"
MODE_CUSTOMER = "customer"
# None / null  → show mode-selection screen

_MODE_TRIGGERS = {"menu", "back", "home", "switch", "mode"}


def _twiml(body: str) -> Response:
    return Response(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Message><Body>{escape(body)}</Body></Message></Response>"
        ),
        media_type="application/xml",
    )


def _twiml_chunks(body: str) -> Response:
    """TwiML response that splits long bodies into multiple <Message> elements."""
    from app.core.twilio_send import _chunk
    chunks = _chunk(body)
    if len(chunks) > 1:
        labeled = [f"[{i+1}/{len(chunks)}]\n{c}" for i, c in enumerate(chunks)]
    else:
        labeled = chunks
    messages = "".join(f"<Message><Body>{escape(c)}</Body></Message>" for c in labeled)
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{messages}</Response>',
        media_type="application/xml",
    )


def _twiml_empty() -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


def _verify(request: Request, params: dict) -> bool:
    from app.core.config import TWILIO_AUTH_TOKEN, TWILIO_VALIDATE_SIGNATURE
    if not TWILIO_VALIDATE_SIGNATURE or not TWILIO_AUTH_TOKEN:
        return True
    try:
        from twilio.request_validator import RequestValidator
        sig = request.headers.get("X-Twilio-Signature", "")
        return RequestValidator(TWILIO_AUTH_TOKEN).validate(str(request.url), params, sig)
    except Exception:
        return True


def _mode_menu(store_name: str) -> str:
    return (
        f"Welcome to {store_name}!\n\n"
        "Reply with:\n"
        "  1 — Staff tools (integrity, revenue, scout, reputation)\n"
        "  2 — Customer app (stamps, deals, loyalty)\n\n"
        "Type *menu* anytime to return here."
    )


def _internal_welcome(store_name: str) -> str:
    return (
        f"Staff tools — {store_name}\n\n"
        "*Integrity* — POS audit & leakage\n"
        "  summary — full overview\n"
        "  leakage — theft & voids breakdown\n"
        "  profit — margins & COGS\n"
        "  staff — per-staff anomalies\n"
        "  daily / weekly — period report\n\n"
        "*Revenue* — Sales & strategy\n"
        "  revenue — overall sales performance\n"
        "  sales — item & category breakdown\n"
        "  pricing — price optimisation tips\n"
        "  strategy — growth recommendations\n\n"
        "*Scout* — Competitor intelligence\n"
        "  scout — scrape rivals (cached 24h)\n\n"
        "*Reputation* — Review management\n"
        "  check — scrape latest reviews (cached 24h)\n"
        "  post — publish suggested reply\n"
        "  ignore — skip current review\n"
        "  edit <text> — rewrite suggested reply\n\n"
        "Type *menu* to switch modes."
    )


def _customer_welcome() -> str:
    return "Switched to customer mode. Send a receipt code or say hi!"


_CSV_CONTENT_TYPES = {
    "text/csv", "text/plain", "application/csv",
    "application/octet-stream", "application/vnd.ms-excel",
}


def _is_csv_upload(params: dict) -> bool:
    if int(params.get("NumMedia", "0")) == 0:
        return False
    ct = params.get("MediaContentType0", "").split(";")[0].strip().lower()
    return ct in _CSV_CONTENT_TYPES


def _detect_file_type_from_caption(caption: str) -> str | None:
    c = caption.lower()
    if any(w in c for w in ("sales", "orders", "transactions")):
        return "pos_sales"
    if any(w in c for w in ("menu", "items", "products", "food")):
        return "pos_menu"
    if any(w in c for w in ("staff", "employees", "team", "crew")):
        return "pos_staff"
    return None


def _detect_file_type_from_headers(content: str) -> str | None:
    import csv as _csv, io as _io
    try:
        headers = {h.strip().lower() for h in next(_csv.reader(_io.StringIO(content)))}
    except Exception:
        return None
    if headers & {"order_id", "is_void", "void_after_fire", "line_amount"}:
        return "pos_sales"
    if headers & {"sku", "price"} and len(headers) <= 8:
        return "pos_menu"
    if headers & {"staff_id", "role"}:
        return "pos_staff"
    return None


async def _handle_csv_upload(store_id: int, from_number: str, params: dict) -> str:
    import httpx
    from datetime import datetime as _dt

    media_url = params.get("MediaUrl0", "")
    caption = (params.get("Body", "") or "").strip()

    file_type = _detect_file_type_from_caption(caption)

    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                media_url,
                auth=(twilio_sid, twilio_token),
                follow_redirects=True,
            )
            resp.raise_for_status()
            content = resp.text
    except Exception as exc:
        logger.error("gateway.csv_upload: store=%d download_failed error=%s", store_id, exc)
        return "Could not download the file. Please try again."

    if file_type is None:
        file_type = _detect_file_type_from_headers(content)

    if file_type is None:
        return (
            "Could not detect the file type. Resend with a caption:\n"
            "  'sales'  for sales/orders data\n"
            "  'menu'   for menu/product data\n"
            "  'staff'  for staff/employee data"
        )

    from app.core.db import SessionLocal, UploadedFile, POSConnection
    from app.agents.integrity.service import get_service

    with SessionLocal() as db:
        row = (
            db.query(UploadedFile)
            .filter(UploadedFile.store_id == store_id, UploadedFile.file_type == file_type)
            .first()
        )
        if row:
            row.content = content
            row.uploaded_by = from_number
            row.uploaded_at = _dt.utcnow()
        else:
            db.add(UploadedFile(
                store_id=store_id,
                file_type=file_type,
                content=content,
                uploaded_by=from_number,
            ))

        pos = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
        if pos is None:
            db.add(POSConnection(
                store_id=store_id,
                pos_type="whatsapp_csv",
                config={"store_id": store_id},
                mapping="cafe_generic",
                currency="PKR",
                timezone="Asia/Karachi",
            ))
        elif pos.pos_type != "whatsapp_csv":
            pos.pos_type = "whatsapp_csv"
            pos.config = {"store_id": store_id}

        db.commit()

    svc = get_service()
    svc._cache.pop(store_id, None)
    svc._data_cache.pop(store_id, None)

    row_count = max(0, len(content.strip().splitlines()) - 1)
    type_label = {"pos_sales": "sales", "pos_menu": "menu", "pos_staff": "staff"}[file_type]
    logger.info(
        "gateway.csv_upload: store=%d type=%s rows=%d from=%s",
        store_id, file_type, row_count, from_number,
    )
    return f"Saved {type_label} data ({row_count} rows). Type 'summary' to run a POS audit."


# ── Async scout dispatch ───────────────────────────────────────────────────────

_SCOUT_ASYNC_WORDS = {
    "scout", "competitor", "competitors", "intel", "intelligence",
    "rivals", "rival", "competition", "landscape",
}


def _is_scout_message(body: str) -> bool:
    words = set(_re.sub(r"[^\w\s]", "", body.lower()).split())
    return bool(words & _SCOUT_ASYNC_WORDS)


# Per-user rate limit: max 3 scout dispatches per 60-minute window (in-memory).
_SCOUT_RATE_WINDOW = 3600   # seconds
_SCOUT_RATE_MAX   = 3
_scout_rate: dict[str, list[float]] = defaultdict(list)  # phone → [timestamps]


def _scout_rate_ok(phone: str) -> bool:
    """Return True (and record the hit) if this user is within the rate limit."""
    now = _time.monotonic()
    hits = [t for t in _scout_rate[phone] if now - t < _SCOUT_RATE_WINDOW]
    if len(hits) >= _SCOUT_RATE_MAX:
        _scout_rate[phone] = hits
        return False
    hits.append(now)
    _scout_rate[phone] = hits
    return True


# ── Spam / duplicate guards ───────────────────────────────────────────────────

# Guard 1: Idempotency dedup — drop duplicate webhook deliveries within 60 s
_IDEM_TTL = 60.0
_seen_idem: dict[str, float] = {}  # key → arrival monotonic time

def _idem_ok(key: str) -> bool:
    """Return True if unseen; False if this key was processed in the last 60 s."""
    now = _time.monotonic()
    expired = [k for k, t in list(_seen_idem.items()) if now - t > _IDEM_TTL]
    for k in expired:
        del _seen_idem[k]
    if key in _seen_idem:
        return False
    _seen_idem[key] = now
    return True


# Guard 2 & 3: Customer in-flight lock + per-phone cooldown
_customer_inflight: set[str] = set()          # phones with an active LLM call
_CUSTOMER_COOLDOWN = 4.0                       # seconds between dispatches
_customer_last: dict[str, float] = {}         # phone → last dispatch monotonic time


def _customer_dispatch_ok(phone: str) -> tuple[bool, str | None]:
    """Check whether a customer message should be dispatched.

    Returns (ok, reason) where reason is 'inflight' | 'cooldown' | None.
    Side-effect: records the dispatch timestamp when ok=True.
    """
    if phone in _customer_inflight:
        return False, "inflight"
    now = _time.monotonic()
    if now - _customer_last.get(phone, 0.0) < _CUSTOMER_COOLDOWN:
        return False, "cooldown"
    _customer_last[phone] = now
    return True, None


# Guard 4: Staff internal command cooldown + in-flight lock
_STAFF_COOLDOWN = 10.0                         # seconds between staff dispatches
_staff_last: dict[str, float] = {}            # phone → last dispatch monotonic time
_staff_inflight: set[str] = set()             # phones with an active internal LLM call


def _staff_dispatch_ok(phone: str) -> bool:
    """Return True and record timestamp if staff command should be dispatched."""
    if phone in _staff_inflight:
        return False
    now = _time.monotonic()
    if now - _staff_last.get(phone, 0.0) < _STAFF_COOLDOWN:
        return False
    _staff_last[phone] = now
    return True


def _send_outbound(to: str, from_: str, body: str) -> None:
    from app.core.twilio_send import send_whatsapp
    send_whatsapp(to=to, body=body, from_=from_)


def _jid_to_internal(jid: str) -> str:
    """Convert OpenWA JID to internal whatsapp: format.
    '923328085405@c.us' -> 'whatsapp:+923328085405'
    """
    bare = jid.split("@")[0]
    if not bare.startswith("+"):
        bare = "+" + bare
    return f"whatsapp:{bare}"


def _internal_to_jid(num: str) -> str:
    """Convert internal whatsapp: format to OpenWA JID.
    'whatsapp:+923328085405' -> '923328085405@c.us'
    """
    bare = num.removeprefix("whatsapp:").lstrip("+")
    return f"{bare}@c.us"


def _resolve_lid(session_id: str, lid_jid: str) -> str | None:
    """Resolve a WhatsApp @lid JID to the real @c.us JID via OpenWA contacts API.

    Returns the @c.us JID string on success, None on failure.
    WhatsApp multi-device uses privacy LIDs instead of phone numbers for some
    contacts; the contacts endpoint maps them back to the real number.
    """
    import httpx
    base_url = os.environ.get("OPENWA_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("OPENWA_API_KEY", "")
    if not base_url or not api_key:
        return None
    try:
        url = f"{base_url}/api/sessions/{session_id}/contacts/{lid_jid}"
        with httpx.Client(timeout=5) as client:
            r = client.get(url, headers={"X-API-Key": api_key})
        if r.status_code == 200:
            contact = r.json()
            real_jid = contact.get("id", "")  # e.g. "923327398165@c.us"
            if real_jid and real_jid.endswith("@c.us"):
                logger.info("openwa.lid_resolve: %s -> %s", lid_jid, real_jid)
                return real_jid
    except Exception as exc:
        logger.warning("openwa.lid_resolve: failed for %s: %s", lid_jid, exc)
    return None


def _bg_scout(store_id: int, from_number: str, send_fn, body: str, ack: str | None = None) -> None:
    """Background task: run scout pipeline, deliver result via provider send_fn.

    ack – if provided, sent immediately before the pipeline runs (OpenWA path).
    """
    if ack:
        send_fn(ack)
    from app.agents.scout.analysis import classify_intent
    from app.agents.scout.pipeline import run as scout_run
    from app.core.db import SessionLocal, Store

    try:
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            store_name = store.name if store else "the restaurant"
            store_category = (store.category or "food") if store else "food"

        command = classify_intent(body, store_name, store_category)
        logger.info("bg_scout: store=%d command=%s from=%s", store_id, command, from_number)

        report = scout_run(command, store_id=store_id, user_message=body)
        send_fn(report)
        logger.info("bg_scout: delivered store=%d", store_id)
        logger.info("bg_scout: reply_text store=%d text=%r", store_id, report)
    except Exception as exc:
        logger.error("bg_scout: store=%d failed error=%s", store_id, exc)
        send_fn("Scout report could not be completed. Please try again.")


_INTEGRITY_SHORTHAND = {
    "summary", "audit", "overview", "leakage", "leak", "theft",
    "profit", "margin", "cogs", "staff", "daily", "weekly",
    "refresh", "pdf", "report",
}
_REVIEW_KEYWORDS = {
    "review", "reviews", "rating", "ratings", "feedback",
    "complaint", "complaints", "comment", "comments", "saying", "people",
}
# "sales" moved here from _INTEGRITY_SHORTHAND -- it's a documented Revenue
# Advisor command (see app/gateway/internal.py's _REVENUE_EXACT), not an
# integrity one; it was giving a "running your POS audit" ack immediately
# before a revenue-advice reply, which read as a mismatched non-sequitur.
_REVENUE_KEYWORDS = {"revenue", "sales", "strategy", "upsell", "growth", "pricing", "campaign"}


def _internal_ack(body: str) -> str:
    """Return an immediate human-readable ack for any internal command."""
    lower = body.lower()
    words = set(_re.sub(r"[^\w\s]", "", lower).split())
    first = lower.split()[0] if lower else ""
    if first in {"check", "scrape", "sync", "crawl"} and not _is_scout_message(body):
        return "Checking your reviews now 🔍 I'll message you when done (30-90 sec)."
    if words & _REVIEW_KEYWORDS:
        return "Pulling up your reviews — give me a sec 👀"
    if words & _INTEGRITY_SHORTHAND:
        return "Running your POS audit — report incoming 📊"
    if words & _REVENUE_KEYWORDS:
        return "On it — checking your sales data now 📈"
    return "On it — I'll message you back shortly."


def _bg_internal(store_id: int, from_number: str, send_fn, body: str, ack: str | None = None) -> None:
    """Background task: handle any internal staff command, deliver via send_fn.

    ack – if provided, sent immediately before processing (used by OpenWA path
          where there is no synchronous TwiML response to carry the ack).
    """
    _staff_inflight.add(from_number)
    if ack:
        send_fn(ack)
    try:
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(from_number, body, store_id)
    except Exception as exc:
        logger.error("bg_internal: store=%d error=%s", store_id, exc)
        reply = "Something went wrong. Please try again."
    finally:
        _staff_inflight.discard(from_number)
    if reply:
        send_fn(reply)
        logger.info("bg_internal: delivered store=%d", store_id)
        logger.info("bg_internal: reply_text store=%d text=%r", store_id, reply)


def _bg_customer(store_id: int, from_number: str, send_fn, body: str) -> None:
    """Background task: handle customer agent message, deliver via send_fn."""
    _customer_inflight.add(from_number)
    try:
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(from_number, body, store_id)
    except Exception as exc:
        logger.error("bg_customer: store=%d error=%s", store_id, exc)
        reply = None
    finally:
        _customer_inflight.discard(from_number)
    if reply:
        send_fn(reply)
        logger.info("bg_customer: delivered store=%d", store_id)
        logger.info("bg_customer: reply_text store=%d text=%r", store_id, reply)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.db import init_db
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    )
    logger.info("Central server started")
    yield


app = FastAPI(title="AsaanPay Central Agent Server", lifespan=lifespan)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    import os
    checks: dict[str, str] = {}

    # DB
    try:
        from app.core.db import SessionLocal
        with SessionLocal() as db:
            db.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"

    # LLM
    checks["llm"] = "ok" if os.environ.get("ZAI_API_KEY") else "error: ZAI_API_KEY not set"

    # Apify (scout + reputation)
    checks["apify"] = "ok" if os.environ.get("APIFY_TOKEN") else "error: APIFY_TOKEN not set"

    # Twilio
    checks["twilio"] = (
        "ok"
        if os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN")
        else "error: Twilio credentials not set"
    )

    # OpenWA (optional — only flagged if base URL is set but key is missing)
    owa_base = os.environ.get("OPENWA_BASE_URL", "")
    owa_key = os.environ.get("OPENWA_API_KEY", "")
    if owa_base and not owa_key:
        checks["openwa"] = "error: OPENWA_BASE_URL set but OPENWA_API_KEY missing"
    elif owa_base and owa_key:
        checks["openwa"] = "ok"
    else:
        checks["openwa"] = "not_configured"

    # Agent imports
    agents = {
        "scout":      "app.agents.scout.pipeline",
        "reputation": "app.agents.reputation",
        "integrity":  "app.agents.integrity.agents.integrity_agent",
        "revenue":    "app.agents.revenue.whatsapp",
        "customer":   "app.agents.customer.agents.community_customer",
    }
    agent_status: dict[str, str] = {}
    for name, module in agents.items():
        try:
            __import__(module)
            agent_status[name] = "ok"
        except Exception as exc:
            agent_status[name] = f"error: {exc}"

    all_ok = all(v == "ok" for v in {**checks, **agent_status}.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "agents": agent_status,
    }


# ── Unified WhatsApp webhook ───────────────────────────────────────────────────

@app.post("/whatsapp")
async def unified_whatsapp(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw = (await request.body()).decode("utf-8")
    params = {k: v[0] for k, v in parse_qs(raw).items()}
    if not _verify(request, params):
        return Response(status_code=403, content="invalid signature")

    from_number = params.get("From", "")
    to_number   = params.get("To", "")
    body        = params.get("Body", "").strip()

    logger.info("gateway.webhook: to=%s from=%s body=%r", to_number, from_number, body[:80])

    # ── Resolve store from the number they texted ──────────────────────────────
    from app.core.db import get_store_by_twilio_number, is_store_member, get_user_session, set_user_session

    store = get_store_by_twilio_number(to_number)
    if store is None:
        logger.warning("gateway.webhook: no store mapped to number=%s", to_number)
        return _twiml_empty()

    store_id   = store.id
    store_name = store.name
    logger.info("gateway.webhook: store=%s(%d) from=%s", store_name, store_id, from_number)

    # ── Build Twilio send_fn ───────────────────────────────────────────────────
    def _twilio_send_fn(reply: str) -> None:
        _send_outbound(to=from_number, from_=to_number, body=reply)

    # ── CSV file upload (staff only) ───────────────────────────────────────────
    if _is_csv_upload(params) and is_store_member(from_number, store_id):
        logger.info("gateway.webhook: csv_upload detected store=%d from=%s", store_id, from_number)
        reply = await _handle_csv_upload(store_id, from_number, params)
        return _twiml(reply)

    # ── Twilio idempotency: deduplicate by MessageSid ─────────────────────────
    msg_sid = params.get("MessageSid", "")
    if msg_sid and not _idem_ok(f"twilio:{msg_sid}"):
        logger.info("gateway.webhook: duplicate MessageSid=%s — dropped", msg_sid)
        return _twiml_empty()

    # ── Check if sender is a whitelisted staff member ──────────────────────────
    if not is_store_member(from_number, store_id):
        ok, reason = _customer_dispatch_ok(from_number)
        if not ok:
            if reason == "inflight":
                logger.info("gateway.webhook: customer inflight drop from=%s", from_number)
                return _twiml("Still working on your last message — almost there! 🙏")
            logger.info("gateway.webhook: customer cooldown drop from=%s", from_number)
            return _twiml_empty()
        background_tasks.add_task(_bg_customer, store_id, from_number, _twilio_send_fn, body)
        return _twiml_empty()

    # ── Staff flow ─────────────────────────────────────────────────────────────
    session = get_user_session(from_number)
    # If the session was set for a different store, treat as first contact here
    current_mode = (
        session.active_agent
        if session and session.store_id == store_id
        else None
    )

    cmd = body.lower().strip()
    first_word = cmd.split()[0] if cmd else ""

    # "menu" / "back" always returns to mode-selection screen
    if cmd in _MODE_TRIGGERS:
        set_user_session(from_number, store_id, active_agent=None)
        return _twiml(_mode_menu(store_name))

    # Reputation action commands work regardless of session state — owners reply
    # to review alerts from any context and must not hit the mode-selection screen.
    if first_word in ("post", "ignore", "done", "exit") or (first_word == "edit" and len(body.split()) > 1):
        logger.info("gateway.webhook: reputation_action=%s store=%d from=%s", first_word, store_id, from_number)
        from app.agents.reputation import process_reputation_owner_reply
        reply = process_reputation_owner_reply(from_number, body, store_id=store_id)
        return _twiml(reply)

    # No mode set — show selection screen; keep showing it until "1" or "2"
    if current_mode is None:
        if cmd == "1":
            set_user_session(from_number, store_id, active_agent=MODE_INTERNAL)
            return _twiml(_internal_welcome(store_name))
        if cmd == "2":
            set_user_session(from_number, store_id, active_agent=MODE_CUSTOMER)
            return _twiml(_customer_welcome())
        # Any other input (including "hi", wrong text) → show selection again
        return _twiml(_mode_menu(store_name))

    # ── Internal tools mode ────────────────────────────────────────────────────
    if current_mode == MODE_INTERNAL:
        logger.info("gateway.webhook: store=%d mode=internal from=%s", store_id, from_number)

        # Scout is long-running (2-10 min) — dispatch async, return immediate ack
        if _is_scout_message(body):
            from app.core.db import SessionLocal, ScoutRun as Run
            from datetime import datetime, timedelta

            # 1. Per-user rate limit (3 per hour)
            if not _scout_rate_ok(from_number):
                return _twiml(
                    "You've sent too many scout requests. Limit is 3 per hour — "
                    "please wait before trying again."
                )

            with SessionLocal() as _db:
                # 2. Block if a run is already in flight for this store
                cutoff_running = datetime.utcnow() - timedelta(minutes=15)
                in_flight = _db.query(Run).filter(
                    Run.store_id == store_id,
                    Run.status == "running",
                    Run.started_at >= cutoff_running,
                ).first()
                if in_flight:
                    return _twiml(
                        "Scout is already running — your report will arrive in a few minutes. Please wait."
                    )

                # 3. Return cached report if last successful run was recent (< 24h)
                cutoff_cache = datetime.utcnow() - timedelta(hours=24)
                cached_run = _db.query(Run).filter(
                    Run.store_id == store_id,
                    Run.status.in_(["ok", "partial"]),
                    Run.finished_at >= cutoff_cache,
                ).order_by(Run.finished_at.desc()).first()

            if cached_run:
                age_min = int((datetime.utcnow() - cached_run.finished_at).total_seconds() / 60)
                logger.info("gateway.webhook: scout_cache_hit store=%d age_min=%d", store_id, age_min)
                from app.core.db import ScoutReport
                with SessionLocal() as _db:
                    saved = _db.query(ScoutReport).filter(
                        ScoutReport.store_id == store_id,
                        ScoutReport.run_id == cached_run.id,
                    ).order_by(ScoutReport.id.desc()).first()
                    cached_text = saved.report_text if saved else None
                if cached_text:
                    logger.info("gateway.webhook: scout_cache_serve store=%d chunks=%d", store_id, len(cached_text) // 1500 + 1)
                    return _twiml_chunks(cached_text)
                # Report text missing in DB — fall through to fresh scan

            logger.info("gateway.webhook: scout_async_dispatch store=%d from=%s", store_id, from_number)
            background_tasks.add_task(_bg_scout, store_id, from_number, _twilio_send_fn, body)
            return _twiml(
                "Scanning competitors across Instagram, Google Maps and their websites 🔍\n"
                "Your report will arrive in 7-10 minutes."
            )

        # Reputation check — serve cache hit via TwiML instantly (same pattern as scout)
        if any(kw in cmd for kw in ("check", "scrape", "crawl", "sync")):
            from app.agents.reputation import check_reputation_cache
            hit, cached_text = check_reputation_cache(store_id, store_name)
            if hit:
                logger.info("gateway.webhook: reputation_cache_serve store=%d", store_id)
                return _twiml_chunks(cached_text)

        # All internal commands (integrity/revenue/reputation) involve LLM calls
        # (10-30s routing + 10-30s response) that exceed Twilio's 15s timeout.
        # Dispatch every command as a background task and ack immediately.
        if not _staff_dispatch_ok(from_number):
            logger.info("gateway.webhook: staff cooldown drop from=%s", from_number)
            return _twiml("Still working on your last request — one moment! 🙏")
        ack = _internal_ack(body)
        logger.info("gateway.webhook: internal_async store=%d from=%s ack=%r", store_id, from_number, ack)
        background_tasks.add_task(_bg_internal, store_id, from_number, _twilio_send_fn, body)
        return _twiml(ack)

    # ── Customer app mode ───────────────────────────────────────────────────────
    logger.info("gateway.webhook: store=%d mode=customer from=%s", store_id, from_number)
    ok, reason = _customer_dispatch_ok(from_number)
    if not ok:
        if reason == "inflight":
            logger.info("gateway.webhook: staff-customer inflight drop from=%s", from_number)
            return _twiml("Still working on your last message — almost there! 🙏")
        logger.info("gateway.webhook: staff-customer cooldown drop from=%s", from_number)
        return _twiml_empty()
    background_tasks.add_task(_bg_customer, store_id, from_number, _twilio_send_fn, body)
    return _twiml_empty()


# ── OpenWA webhook ────────────────────────────────────────────────────────────
#
# OpenWA POSTs JSON to this endpoint when a message is received on any of the
# registered sessions.  Unlike the Twilio webhook there is no synchronous
# TwiML reply — we return 200 immediately and send all responses via the
# OpenWA REST API (send_fn wraps openwa_send.send_openwa).
#
# Payload shape (OpenWA webhook.service.ts):
#   {
#     "event":          "message.received",
#     "timestamp":      "2026-...",
#     "sessionId":      "<session-id>",
#     "idempotencyKey": "...",
#     "deliveryId":     "...",
#     "data": {
#       "from":    "923328085405@c.us",   # sender JID
#       "to":      "16292595668@c.us",    # session's own JID
#       "body":    "hello",
#       "type":    "chat",
#       "fromMe":  false,
#       "isGroup": false,
#       ...
#     }
#   }
#
# Configure OpenWA to call this endpoint:
#   POST /api/sessions/{sessionId}/webhooks
#   { "url": "https://asaanintelligence.up.railway.app/openwa/webhook",
#     "events": ["message.received"] }

@app.post("/openwa/webhook")
async def openwa_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    import json as _json

    raw_bytes = await request.body()
    try:
        payload = _json.loads(raw_bytes)
    except Exception:
        logger.warning("openwa.webhook: non-JSON body received")
        return JSONResponse({"status": "bad_request"}, status_code=400)

    logger.info("openwa.webhook: raw=%s", _json.dumps(payload)[:2000])

    event = payload.get("event", "")
    if event != "message.received":
        return JSONResponse({"status": "ignored", "event": event})

    session_id = payload.get("sessionId", "")
    data = payload.get("data", {})

    body_text = (data.get("body") or "").strip()
    from_jid = data.get("from", "")
    from_me = data.get("fromMe", False)
    is_group = data.get("isGroup", False)
    msg_type = data.get("type", "chat")

    # Ignore our own sent messages, group chats, and non-text messages
    if from_me or is_group or not body_text or msg_type not in ("chat", "text", ""):
        return JSONResponse({"status": "ignored"})

    # Idempotency: drop duplicate deliveries within 60 s
    idem_key = payload.get("idempotencyKey") or payload.get("deliveryId") or ""
    if idem_key and not _idem_ok(f"owa:{idem_key}"):
        logger.info("openwa.webhook: duplicate idempotencyKey=%s — dropped", idem_key)
        return JSONResponse({"status": "duplicate"})

    # WhatsApp multi-device sends @lid (privacy ID) instead of @c.us (phone) for
    # some contacts. Resolve to the real @c.us JID so is_store_member() can match
    # against phone numbers stored in store_members.
    resolved_jid = from_jid
    if from_jid.endswith("@lid"):
        resolved_jid = _resolve_lid(session_id, from_jid) or from_jid

    from_number = _jid_to_internal(resolved_jid)
    logger.info("openwa.webhook: session=%s from=%s (raw_jid=%s) body=%r", session_id, from_number, from_jid, body_text[:80])

    from app.core.db import get_store_by_openwa_session, is_store_member, get_user_session, set_user_session

    store = get_store_by_openwa_session(session_id)
    if store is None:
        logger.warning("openwa.webhook: unknown session=%s", session_id)
        return JSONResponse({"status": "unknown_session"}, status_code=404)

    store_id = store.id
    store_name = store.name
    logger.info("openwa.webhook: store=%s(%d) from=%s", store_name, store_id, from_number)

    # Build provider-specific send function — reply to resolved @c.us JID, not LID
    from app.core.openwa_send import send_openwa as _openwa_send_fn
    def _owa_send(reply: str) -> None:
        _openwa_send_fn(session_id, resolved_jid, reply)

    # ── Customer path ──────────────────────────────────────────────────────────
    if not is_store_member(from_number, store_id):
        ok, reason = _customer_dispatch_ok(from_number)
        if not ok:
            if reason == "inflight":
                logger.info("openwa.webhook: customer inflight drop from=%s", from_number)
                background_tasks.add_task(_owa_send, "Still working on your last message — almost there! 🙏")
            else:
                logger.info("openwa.webhook: customer cooldown drop from=%s", from_number)
            return JSONResponse({"status": "throttled"})
        background_tasks.add_task(_bg_customer, store_id, from_number, _owa_send, body_text)
        return JSONResponse({"status": "ok"})

    # ── Staff path ─────────────────────────────────────────────────────────────
    session = get_user_session(from_number)
    current_mode = (
        session.active_agent
        if session and session.store_id == store_id
        else None
    )

    cmd = body_text.lower().strip()
    first_word = cmd.split()[0] if cmd else ""

    if cmd in _MODE_TRIGGERS:
        set_user_session(from_number, store_id, active_agent=None)
        background_tasks.add_task(_owa_send, _mode_menu(store_name))
        return JSONResponse({"status": "ok"})

    if first_word in ("post", "ignore", "done", "exit") or (first_word == "edit" and len(body_text.split()) > 1):
        logger.info("openwa.webhook: reputation_action=%s store=%d from=%s", first_word, store_id, from_number)
        def _do_reputation_action() -> None:
            from app.agents.reputation import process_reputation_owner_reply
            reply = process_reputation_owner_reply(from_number, body_text, store_id=store_id)
            _owa_send(reply)
        background_tasks.add_task(_do_reputation_action)
        return JSONResponse({"status": "ok"})

    if current_mode is None:
        if cmd == "1":
            set_user_session(from_number, store_id, active_agent=MODE_INTERNAL)
            background_tasks.add_task(_owa_send, _internal_welcome(store_name))
        elif cmd == "2":
            set_user_session(from_number, store_id, active_agent=MODE_CUSTOMER)
            background_tasks.add_task(_owa_send, _customer_welcome())
        else:
            # Any other input (including "hi", wrong text) → show selection again
            background_tasks.add_task(_owa_send, _mode_menu(store_name))
        return JSONResponse({"status": "ok"})

    # ── Internal tools mode ────────────────────────────────────────────────────
    if current_mode == MODE_INTERNAL:
        logger.info("openwa.webhook: store=%d mode=internal from=%s", store_id, from_number)

        if _is_scout_message(body_text):
            from app.core.db import SessionLocal, ScoutRun as Run
            from datetime import datetime, timedelta

            if not _scout_rate_ok(from_number):
                background_tasks.add_task(
                    _owa_send,
                    "You've sent too many scout requests. Limit is 3 per hour — please wait.",
                )
                return JSONResponse({"status": "ok"})

            with SessionLocal() as _db:
                cutoff_running = datetime.utcnow() - timedelta(minutes=15)
                in_flight = _db.query(Run).filter(
                    Run.store_id == store_id,
                    Run.status == "running",
                    Run.started_at >= cutoff_running,
                ).first()
                if in_flight:
                    background_tasks.add_task(
                        _owa_send,
                        "Scout is already running — your report will arrive in a few minutes.",
                    )
                    return JSONResponse({"status": "ok"})

                cutoff_cache = datetime.utcnow() - timedelta(hours=24)
                cached_run = _db.query(Run).filter(
                    Run.store_id == store_id,
                    Run.status.in_(["ok", "partial"]),
                    Run.finished_at >= cutoff_cache,
                ).order_by(Run.finished_at.desc()).first()

            if cached_run:
                age_min = int((datetime.utcnow() - cached_run.finished_at).total_seconds() / 60)
                logger.info("openwa.webhook: scout_cache_hit store=%d age_min=%d", store_id, age_min)
                from app.core.db import ScoutReport
                with SessionLocal() as _db:
                    saved = _db.query(ScoutReport).filter(
                        ScoutReport.store_id == store_id,
                        ScoutReport.run_id == cached_run.id,
                    ).order_by(ScoutReport.id.desc()).first()
                    cached_text = saved.report_text if saved else None
                if cached_text:
                    logger.info("openwa.webhook: scout_cache_serve store=%d", store_id)
                    background_tasks.add_task(_owa_send, cached_text)
                    return JSONResponse({"status": "ok"})

            logger.info("openwa.webhook: scout_async_dispatch store=%d from=%s", store_id, from_number)
            scout_ack = (
                "Scanning competitors across Instagram, Google Maps and their websites 🔍\n"
                "Your report will arrive in 7-10 minutes."
            )
            background_tasks.add_task(_bg_scout, store_id, from_number, _owa_send, body_text, ack=scout_ack)
            return JSONResponse({"status": "ok"})

        # Reputation cache check
        if any(kw in cmd for kw in ("check", "scrape", "crawl", "sync")):
            from app.agents.reputation import check_reputation_cache
            hit, cached_text = check_reputation_cache(store_id, store_name)
            if hit:
                logger.info("openwa.webhook: reputation_cache_serve store=%d", store_id)
                background_tasks.add_task(_owa_send, cached_text)
                return JSONResponse({"status": "ok"})

        if not _staff_dispatch_ok(from_number):
            logger.info("openwa.webhook: staff cooldown drop from=%s", from_number)
            background_tasks.add_task(_owa_send, "Still working on your last request — one moment! 🙏")
            return JSONResponse({"status": "throttled"})
        ack = _internal_ack(body_text)
        logger.info("openwa.webhook: internal_async store=%d from=%s ack=%r", store_id, from_number, ack)
        background_tasks.add_task(_bg_internal, store_id, from_number, _owa_send, body_text, ack)
        return JSONResponse({"status": "ok"})

    # ── Customer app mode ──────────────────────────────────────────────────────
    logger.info("openwa.webhook: store=%d mode=customer from=%s", store_id, from_number)
    ok, reason = _customer_dispatch_ok(from_number)
    if not ok:
        if reason == "inflight":
            logger.info("openwa.webhook: staff-customer inflight drop from=%s", from_number)
            background_tasks.add_task(_owa_send, "Still working on your last message — almost there! 🙏")
        else:
            logger.info("openwa.webhook: staff-customer cooldown drop from=%s", from_number)
        return JSONResponse({"status": "throttled"})
    background_tasks.add_task(_bg_customer, store_id, from_number, _owa_send, body_text)
    return JSONResponse({"status": "ok"})


# ── Integrity PDF report ───────────────────────────────────────────────────────

@app.get("/report/{store_id}.pdf")
async def integrity_pdf(store_id: int) -> Response:
    from app.agents.integrity.service import get_service
    from app.agents.integrity.report.pdf import build_audit_pdf
    from app.agents.integrity.agents.integrity_agent import run_integrity_agent
    from app.agents.integrity.pos.base import build_connector

    svc = get_service()
    try:
        config = svc._get_config(store_id)
        if not config:
            return Response(status_code=404, content="no POS configured for this store")
        data = build_connector(config).fetch()
        r = run_integrity_agent(data.orders, data.menu, data.staff,
                                venue_name=config.venue_name, use_llm=False)
        pdf = build_audit_pdf(r.integrity, r.reconciliation,
                              venue_name=r.venue_name, summary=r.executive_summary)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="audit_{store_id}.pdf"'})
    except FileNotFoundError:
        return Response(status_code=404, content="POS not configured")


# ── Admin endpoints ────────────────────────────────────────────────────────────

@app.post("/admin/chains", status_code=201)
async def create_chain(request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Chain
    params = await _parse_body(request)
    name = str(params.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    with SessionLocal() as db:
        chain = Chain(name=name)
        db.add(chain)
        db.commit()
        db.refresh(chain)
        return JSONResponse({"id": chain.id, "name": chain.name}, status_code=201)


@app.post("/admin/stores", status_code=201)
async def create_store(request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store
    params = await _parse_body(request)
    name = str(params.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    with SessionLocal() as db:
        chain_id_val = params.get("chain_id")
        store = Store(
            name=name,
            chain_id=int(chain_id_val) if chain_id_val else None,
            location=params.get("location") or None,
            category=params.get("category") or None,
            instagram_handle=params.get("instagram_handle") or None,
        )
        db.add(store)
        db.commit()
        store_id = store.id
        store_name = store.name
    return JSONResponse({"id": store_id, "name": store_name}, status_code=201)


@app.post("/admin/stores/{store_id}/members", status_code=201)
async def add_member(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreMember
    params = await _parse_body(request)
    raw = str(params.get("whatsapp", "")).strip()
    whatsapp = raw if raw.startswith("whatsapp:") else f"whatsapp:{raw}"
    bare = whatsapp[len("whatsapp:"):]  # e.g. "+16292595668"
    role = params.get("role", "owner")
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        # Remove any un-prefixed duplicate for this number
        db.query(StoreMember).filter(
            StoreMember.store_id == store_id,
            StoreMember.whatsapp == bare,
        ).delete()
        exists = db.query(StoreMember).filter(
            StoreMember.store_id == store_id, StoreMember.whatsapp == whatsapp,
        ).first()
        if exists:
            db.commit()
            return JSONResponse({"status": "already_exists"}, status_code=200)
        db.add(StoreMember(store_id=store_id, whatsapp=whatsapp, role=role))
        db.commit()
    return JSONResponse({"status": "added", "store_id": store_id, "whatsapp": whatsapp}, status_code=201)


@app.delete("/admin/stores/{store_id}/members", status_code=200)
async def remove_member(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, StoreMember
    params = await _parse_body(request)
    raw = str(params.get("whatsapp", "")).strip()
    whatsapp = raw if raw.startswith("whatsapp:") else f"whatsapp:{raw}"
    bare = whatsapp[len("whatsapp:"):]
    with SessionLocal() as db:
        deleted = db.query(StoreMember).filter(
            StoreMember.store_id == store_id,
            StoreMember.whatsapp.in_([whatsapp, bare]),
        ).delete(synchronize_session=False)
        db.commit()
    if deleted:
        return JSONResponse({"status": "removed", "count": deleted})
    return JSONResponse({"status": "not_found"}, status_code=404)


@app.post("/admin/stores/{store_id}/locations", status_code=201)
async def add_location(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreLocation
    params = await _parse_body(request)
    address = str(params.get("address", "")).strip()
    if not address:
        return JSONResponse({"error": "address required"}, status_code=400)
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        db.add(StoreLocation(
            store_id=store_id, address=address,
            city=params.get("city") or None, area=params.get("area") or None,
            is_primary=params.get("is_primary", "false"),
        ))
        db.commit()
    return JSONResponse({"status": "added", "store_id": store_id, "address": address}, status_code=201)


@app.post("/admin/stores/{store_id}/pos", status_code=201)
async def configure_pos(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, POSConnection
    from app.agents.integrity.service import get_service
    params = await _parse_body(request)
    config_val = params.get("config", {})
    if isinstance(config_val, str):
        try:
            config_json = _json_mod.loads(config_val)
        except Exception:
            return JSONResponse({"error": "config must be valid JSON"}, status_code=400)
    else:
        config_json = config_val or {}
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        existing = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
        if existing:
            existing.pos_type = params.get("pos_type", "csv")
            existing.config = config_json
            existing.mapping = params.get("mapping", "cafe_generic")
            existing.currency = params.get("currency", "PKR")
            existing.timezone = params.get("timezone", "Asia/Karachi")
        else:
            db.add(POSConnection(
                store_id=store_id,
                pos_type=params.get("pos_type", "csv"),
                config=config_json,
                mapping=params.get("mapping", "cafe_generic"),
                currency=params.get("currency", "PKR"),
                timezone=params.get("timezone", "Asia/Karachi"),
            ))
        db.commit()
    get_service()._cache.pop(store_id, None)
    return JSONResponse({"status": "configured", "store_id": store_id}, status_code=201)


@app.post("/admin/stores/{store_id}/revenue", status_code=201)
async def configure_revenue(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, RevenueConnection
    from app.agents.revenue.registry import get_registry
    params = await _parse_body(request)
    config_val = params.get("config", {})
    config_json = config_val if isinstance(config_val, dict) else {}
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        existing = db.query(RevenueConnection).filter(RevenueConnection.store_id == store_id).first()
        if existing:
            existing.data_dir = params.get("data_dir") or existing.data_dir
            existing.db_path = params.get("db_path") or existing.db_path
            existing.config = config_json
        else:
            db.add(RevenueConnection(
                store_id=store_id,
                data_dir=params.get("data_dir") or None,
                db_path=params.get("db_path") or ":memory:",
                config=config_json,
            ))
        db.commit()
    get_registry().invalidate(store_id)
    return JSONResponse({"status": "configured", "store_id": store_id}, status_code=201)


@app.post("/admin/stores/{store_id}/twilio", status_code=201)
async def set_twilio_number(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreTwilioNumber
    params = await _parse_body(request)
    number = str(params.get("whatsapp_number", "")).strip()
    if not number:
        return JSONResponse({"error": "whatsapp_number required"}, status_code=400)
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        existing = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == store_id).first()
        if existing:
            existing.whatsapp_number = number
        else:
            db.add(StoreTwilioNumber(store_id=store_id, whatsapp_number=number))
        db.commit()
    return JSONResponse({"status": "set", "store_id": store_id, "whatsapp_number": number}, status_code=201)


@app.post("/admin/stores/{store_id}/openwa", status_code=201)
async def set_openwa_session(store_id: int, request: Request) -> JSONResponse:
    """Register (or update) an OpenWA session for a store.

    Body: { "session_id": "anatummy-wa", "phone_number": "+923XXXXXXXXX" }
    session_id  – the session name in the OpenWA dashboard
    phone_number – the WhatsApp number scanned into that session (E.164)
    """
    from app.core.db import SessionLocal, Store, StoreOpenWASession
    params = await _parse_body(request)
    session_id = str(params.get("session_id", "")).strip()
    phone_number = str(params.get("phone_number", "")).strip()
    if not session_id or not phone_number:
        return JSONResponse({"error": "session_id and phone_number required"}, status_code=400)
    if not phone_number.startswith("+"):
        phone_number = "+" + phone_number
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        existing = db.query(StoreOpenWASession).filter(StoreOpenWASession.store_id == store_id).first()
        if existing:
            existing.session_id = session_id
            existing.phone_number = phone_number
        else:
            db.add(StoreOpenWASession(store_id=store_id, session_id=session_id, phone_number=phone_number))
        db.commit()
    return JSONResponse(
        {"status": "set", "store_id": store_id, "session_id": session_id, "phone_number": phone_number},
        status_code=201,
    )


@app.delete("/admin/stores/{store_id}/openwa", status_code=200)
async def remove_openwa_session(store_id: int) -> JSONResponse:
    """Remove the OpenWA session registration for a store (e.g. after Twilio goes live)."""
    from app.core.db import SessionLocal, StoreOpenWASession
    with SessionLocal() as db:
        deleted = db.query(StoreOpenWASession).filter(
            StoreOpenWASession.store_id == store_id
        ).delete(synchronize_session=False)
        db.commit()
    if deleted:
        return JSONResponse({"status": "removed"})
    return JSONResponse({"status": "not_found"}, status_code=404)


@app.post("/admin/debug/kb-upsert/{store_id}")
async def debug_kb_upsert(store_id: int, request: Request) -> JSONResponse:
    """TEMPORARY — insert new / update existing KB chunks with real embeddings.
    Body: {"chunks": [{"id": "<uuid, optional>", "content": "...", "metadata": {...}}]}
    id present -> UPDATE that row's content/embedding/metadata.
    id absent  -> INSERT a new row for store_id.
    Remove after use; not meant to be a permanent endpoint."""
    import json as _json
    from sqlalchemy import text as _sql
    from app.core.db import SessionLocal
    from app.agents.customer.community.store import _embedding_model
    import app.core.cache as _cache

    params = await _parse_body(request)
    chunks = params.get("chunks", [])
    model = _embedding_model()
    if model is None:
        return JSONResponse({"error": "embedding model unavailable"}, status_code=500)

    results = []
    with SessionLocal() as db:
        for c in chunks:
            embedding = model.encode(c["content"]).tolist()
            metadata = c.get("metadata", {})
            if c.get("id"):
                db.execute(
                    _sql(
                        "UPDATE knowledge_base SET content=:content, "
                        "embedding=CAST(:embedding AS vector), metadata=CAST(:metadata AS jsonb) "
                        "WHERE id=:id"
                    ),
                    {"content": c["content"], "embedding": _json.dumps(embedding),
                     "metadata": _json.dumps(metadata), "id": c["id"]},
                )
                results.append({"id": c["id"], "action": "updated"})
            else:
                db.execute(
                    _sql(
                        "INSERT INTO knowledge_base (store_id, content, embedding, metadata) "
                        "VALUES (:store_id, :content, CAST(:embedding AS vector), CAST(:metadata AS jsonb))"
                    ),
                    {"store_id": store_id, "content": c["content"],
                     "embedding": _json.dumps(embedding), "metadata": _json.dumps(metadata)},
                )
                results.append({"action": "inserted"})
        db.commit()

    for cat in ("menu", "hours", "location", "delivery", "about"):
        _cache.delete(f"kb:{store_id}:{cat}")

    return JSONResponse({"status": "ok", "results": results})


@app.post("/admin/stores/{store_id}/customer")
async def configure_venue(store_id: int, request: Request) -> JSONResponse:
    import json as _json
    from datetime import datetime as _dt
    from app.core.db import SessionLocal, Store, VenueConfig as OrmVenueConfig
    params = await _parse_body(request)
    try:
        owner_phones = params.get("owner_phones", [])
        if isinstance(owner_phones, str):
            owner_phones = _json.loads(owner_phones)
    except Exception:
        owner_phones = []
    try:
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            if not store:
                return JSONResponse({"error": "store not found"}, status_code=404)
            row = db.query(OrmVenueConfig).filter(OrmVenueConfig.store_id == store_id).first()
            if row is None:
                row = OrmVenueConfig(store_id=store_id, venue_name=params.get("venue_name", store.name))
                db.add(row)
            row.venue_name = params.get("venue_name", store.name)
            row.stamp_goal = int(params.get("stamp_goal", 5))
            row.reward_text = params.get("reward_text", "a free drink or dessert")
            row.winback_days = int(params.get("winback_days", 5))
            row.code_expiry_days = int(params.get("code_expiry_days", 30))
            row.owner_phones = owner_phones
            row.qr_greeting = params.get("qr_greeting") or ""
            row.updated_at = _dt.utcnow()
            db.commit()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"status": "configured", "store_id": store_id})


@app.post("/admin/stores/{store_id}/reputation", status_code=201)
async def configure_reputation(store_id: int, request: Request) -> JSONResponse:
    """Set per-store reputation scraping targets and brand voice.

    Body (all optional — send only what you want to update):
      google_maps_terms    JSON array  ["Venue Name", "Venue Name City"]
      google_maps_location string      "City, Country"
      foodpanda_url        string
      foodpanda_keyword    string
      instagram_usernames  JSON array  ["handle1"]
      brand_voice_tone     string      "Warm, genuine, professional"
      brand_voice_never_say JSON array ["unfortunately"]
    """
    import json as _json
    from datetime import datetime as _dt
    from app.core.db import SessionLocal, Store, ReputationConfig

    params = await _parse_body(request)

    def _parse_list(key: str) -> list:
        val = params.get(key, [])
        if isinstance(val, str):
            try:
                return _json.loads(val)
            except Exception:
                return [v.strip() for v in val.split(",") if v.strip()]
        return val or []

    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)

        rc = db.query(ReputationConfig).filter(ReputationConfig.store_id == store_id).first()
        if rc is None:
            rc = ReputationConfig(store_id=store_id)
            db.add(rc)

        if "google_maps_terms" in params:
            rc.google_maps_terms = _parse_list("google_maps_terms")
        if "google_maps_location" in params:
            rc.google_maps_location = str(params["google_maps_location"]).strip() or None
        if "foodpanda_url" in params:
            rc.foodpanda_url = str(params["foodpanda_url"]).strip() or None
        if "foodpanda_keyword" in params:
            rc.foodpanda_keyword = str(params["foodpanda_keyword"]).strip() or None
        if "instagram_usernames" in params:
            rc.instagram_usernames = _parse_list("instagram_usernames")
        if "brand_voice_tone" in params:
            rc.brand_voice_tone = str(params["brand_voice_tone"]).strip() or None
        if "brand_voice_never_say" in params:
            rc.brand_voice_never_say = _parse_list("brand_voice_never_say")

        rc.updated_at = _dt.utcnow()
        db.commit()

    return JSONResponse({"status": "configured", "store_id": store_id}, status_code=201)


@app.post("/admin/stores/{store_id}/seed-competitors")
async def seed_competitors(store_id: int) -> JSONResponse:
    """Seed the default competitor list from config into the DB for this store."""
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
    from app.agents.scout.discovery import seed_competitors_for_store
    added = seed_competitors_for_store(store_id)
    return JSONResponse({"status": "seeded", "store_id": store_id, "added": added})


@app.get("/admin/stores")
async def list_stores() -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreTwilioNumber
    with SessionLocal() as db:
        stores = db.query(Store).all()
        result = []
        for s in stores:
            twilio = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == s.id).first()
            result.append({
                "id": s.id, "name": s.name, "chain_id": s.chain_id,
                "category": s.category, "location": s.location,
                "whatsapp_number": twilio.whatsapp_number if twilio else None,
            })
    return JSONResponse(result)


@app.get("/admin/stores/{store_id}/enroll-link")
async def get_enroll_link(store_id: int) -> JSONResponse:
    from urllib.parse import quote
    from app.core.db import SessionLocal, StoreTwilioNumber, VenueConfig
    with SessionLocal() as db:
        twilio = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == store_id).first()
        vc     = db.query(VenueConfig).filter(VenueConfig.store_id == store_id).first()
    if not twilio:
        return JSONResponse({"error": "no Twilio number configured for this store"}, status_code=404)
    digits = twilio.whatsapp_number.replace("whatsapp:", "").replace("+", "")
    greeting = (vc.qr_greeting if vc and vc.qr_greeting else "Hi! I'd like to join the loyalty programme.")
    url = f"https://wa.me/{digits}?text={quote(greeting)}"
    return JSONResponse({"store_id": store_id, "enroll_url": url, "greeting": greeting})


@app.get("/admin/stores/{store_id}")
async def get_store(store_id: int) -> JSONResponse:
    from app.core.db import (
        SessionLocal, Store, StoreMember, StoreTwilioNumber, POSConnection,
        RevenueConnection, StoreOpenWASession,
    )
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            return JSONResponse({"error": "not found"}, status_code=404)
        members  = db.query(StoreMember).filter(StoreMember.store_id == store_id).all()
        twilio   = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == store_id).first()
        owa      = db.query(StoreOpenWASession).filter(StoreOpenWASession.store_id == store_id).first()
        pos      = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
        rev      = db.query(RevenueConnection).filter(RevenueConnection.store_id == store_id).first()
    return JSONResponse({
        "id": store.id, "name": store.name, "chain_id": store.chain_id,
        "whatsapp_number": twilio.whatsapp_number if twilio else None,
        "openwa_session": {"session_id": owa.session_id, "phone_number": owa.phone_number} if owa else None,
        "members": [{"whatsapp": m.whatsapp, "role": m.role} for m in members],
        "pos_configured": pos is not None,
        "revenue_configured": rev is not None,
    })


@app.post("/admin/stores/{store_id}/rebuild-embeddings")
async def rebuild_embeddings(store_id: int) -> JSONResponse:
    import json as _json
    from sqlalchemy import text as _sql
    from app.core.db import SessionLocal
    from app.agents.customer.community.store import _embedding_model
    model = _embedding_model()
    if not model:
        return JSONResponse({"error": "embedding model unavailable"}, status_code=503)
    with SessionLocal() as db:
        rows = db.execute(_sql(
            "SELECT id, content FROM knowledge_base WHERE store_id = :sid AND embedding IS NULL"
        ), {"sid": store_id}).fetchall()
        for row in rows:
            emb = model.encode(row[1]).tolist()
            db.execute(_sql(
                "UPDATE knowledge_base SET embedding = CAST(:e AS vector) WHERE id = :id"
            ), {"e": _json.dumps(emb), "id": row[0]})
        db.commit()
    return JSONResponse({"rebuilt": len(rows), "store_id": store_id})
