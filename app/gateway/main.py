"""Central server — single FastAPI app, single WhatsApp webhook.

One Twilio number per restaurant branch. The server checks whether the
sender is a whitelisted staff member of that branch:

  • Staff   → mode-selection screen (1 = internal tools, 2 = customer app)
             — or whichever mode they're already in.
  • Customer → straight to the loyalty / community agent.

Staff can type "menu" at any time to return to the mode-selection screen.
Within internal mode, messages are routed transparently to scout, integrity,
or revenue based on keyword — no agent-selection needed from the user.

Webhook:
  POST /whatsapp

Admin endpoints (no auth — add middleware before production):
  POST /admin/chains
  POST /admin/stores
  POST /admin/stores/{id}/members
  POST /admin/stores/{id}/locations
  POST /admin/stores/{id}/pos
  POST /admin/stores/{id}/revenue
  POST /admin/stores/{id}/customer
  POST /admin/stores/{id}/twilio
  GET  /admin/stores
  GET  /admin/stores/{id}
  GET  /health
  GET  /report/{store_id}.pdf
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

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
        "  1 — Staff tools (audit, revenue, scout)\n"
        "  2 — Customer app (stamps, deals, loyalty)\n\n"
        "Type *menu* anytime to return here."
    )


def _internal_welcome(store_name: str) -> str:
    return (
        f"Staff tools — {store_name}\n\n"
        "Integrity: summary · audit · leakage · profit · staff · daily · weekly\n"
        "Revenue:   revenue · sales · pricing · strategy\n"
        "Scout:     scout\n\n"
        "Type *menu* to switch modes."
    )


def _customer_welcome() -> str:
    return "Switched to customer mode. Send a receipt code or say hi!"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.db import init_db
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("Central server started")
    yield


app = FastAPI(title="AsaanPay Central Agent Server", lifespan=lifespan)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "agents": ["scout", "integrity", "revenue", "customer"]}


# ── Unified WhatsApp webhook ───────────────────────────────────────────────────

@app.post("/whatsapp")
async def unified_whatsapp(request: Request) -> Response:
    raw = (await request.body()).decode("utf-8")
    params = {k: v[0] for k, v in parse_qs(raw).items()}
    if not _verify(request, params):
        return Response(status_code=403, content="invalid signature")

    from_number = params.get("From", "")
    to_number   = params.get("To", "")
    body        = params.get("Body", "").strip()

    logger.info("Webhook: to=%s from=%s body=%r", to_number, from_number, body[:80])

    # ── Resolve store from the number they texted ──────────────────────────────
    from app.core.db import get_store_by_twilio_number, is_store_member, get_user_session, set_user_session

    store = get_store_by_twilio_number(to_number)
    if store is None:
        logger.warning("No store mapped to number %s", to_number)
        return _twiml_empty()

    store_id   = store.id
    store_name = store.name

    # ── Check if sender is a whitelisted staff member ──────────────────────────
    if not is_store_member(from_number, store_id):
        # Pure customer — no mode selection, go straight to community agent
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(from_number, body, store_id)
        return _twiml(reply) if reply else _twiml_empty()

    # ── Staff flow ─────────────────────────────────────────────────────────────
    session = get_user_session(from_number)
    current_mode = session.active_agent if session else None

    cmd = body.lower().strip()

    # "menu" / "back" always returns to mode-selection screen
    if cmd in _MODE_TRIGGERS:
        set_user_session(from_number, store_id, active_agent=None)
        return _twiml(_mode_menu(store_name))

    # No mode set — show selection or parse "1"/"2"
    if current_mode is None:
        if cmd == "1":
            set_user_session(from_number, store_id, active_agent=MODE_INTERNAL)
            return _twiml(_internal_welcome(store_name))
        if cmd == "2":
            set_user_session(from_number, store_id, active_agent=MODE_CUSTOMER)
            return _twiml(_customer_welcome())
        # Any other message (including first contact) → show menu
        set_user_session(from_number, store_id, active_agent=None)
        return _twiml(_mode_menu(store_name))

    # ── Internal tools mode ────────────────────────────────────────────────────
    if current_mode == MODE_INTERNAL:
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(from_number, body, store_id)
        return _twiml(reply) if reply else _twiml_empty()

    # ── Customer app mode (staff using loyalty features) ───────────────────────
    from app.gateway.customer import handle_customer_for_store
    reply = handle_customer_for_store(from_number, body, store_id)
    return _twiml(reply) if reply else _twiml_empty()


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

@app.post("/admin/chains")
async def create_chain(request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Chain
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    name = params.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    with SessionLocal() as db:
        chain = Chain(name=name)
        db.add(chain)
        db.commit()
        db.refresh(chain)
        return JSONResponse({"chain_id": chain.id, "name": chain.name})


@app.post("/admin/stores")
async def create_store(request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    name = params.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    with SessionLocal() as db:
        store = Store(
            name=name,
            chain_id=int(params["chain_id"]) if params.get("chain_id") else None,
            location=params.get("location") or None,
            category=params.get("category") or None,
            instagram_handle=params.get("instagram_handle") or None,
        )
        db.add(store)
        db.commit()
        store_id = store.id
        store_name = store.name
    return JSONResponse({"store_id": store_id, "name": store_name})


@app.post("/admin/stores/{store_id}/members")
async def add_member(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreMember
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    whatsapp = params.get("whatsapp", "").strip()
    role = params.get("role", "owner")
    with SessionLocal() as db:
        if not db.query(Store).filter(Store.id == store_id).first():
            return JSONResponse({"error": "store not found"}, status_code=404)
        exists = db.query(StoreMember).filter(
            StoreMember.store_id == store_id, StoreMember.whatsapp == whatsapp,
        ).first()
        if exists:
            return JSONResponse({"status": "already_exists"})
        db.add(StoreMember(store_id=store_id, whatsapp=whatsapp, role=role))
        db.commit()
    return JSONResponse({"status": "added", "store_id": store_id, "whatsapp": whatsapp})


@app.post("/admin/stores/{store_id}/locations")
async def add_location(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreLocation
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    address = params.get("address", "").strip()
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
    return JSONResponse({"status": "added", "store_id": store_id, "address": address})


@app.post("/admin/stores/{store_id}/pos")
async def configure_pos(store_id: int, request: Request) -> JSONResponse:
    import json as _json
    from app.core.db import SessionLocal, Store, POSConnection
    from app.agents.integrity.service import get_service
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    try:
        config_json = _json.loads(params.get("config", "{}"))
    except Exception:
        return JSONResponse({"error": "config must be valid JSON"}, status_code=400)
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
    return JSONResponse({"status": "configured", "store_id": store_id})


@app.post("/admin/stores/{store_id}/revenue")
async def configure_revenue(store_id: int, request: Request) -> JSONResponse:
    import json as _json
    from app.core.db import SessionLocal, Store, RevenueConnection
    from app.agents.revenue.registry import get_registry
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    try:
        config_json = _json.loads(params.get("config", "{}"))
    except Exception:
        return JSONResponse({"error": "config must be valid JSON"}, status_code=400)
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
    return JSONResponse({"status": "configured", "store_id": store_id})


@app.post("/admin/stores/{store_id}/twilio")
async def set_twilio_number(store_id: int, request: Request) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreTwilioNumber
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    number = params.get("whatsapp_number", "").strip()
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
    return JSONResponse({"status": "set", "store_id": store_id, "whatsapp_number": number})


@app.post("/admin/stores/{store_id}/customer")
async def configure_venue(store_id: int, request: Request) -> JSONResponse:
    import json as _json
    from app.core.db import SessionLocal, Store
    params = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    try:
        owner_phones = _json.loads(params.get("owner_phones", "[]"))
    except Exception:
        owner_phones = []
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"],
                           os.environ.get("SUPABASE_SERVICE_KEY", os.environ.get("SUPABASE_KEY", "")))
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            if not store:
                return JSONResponse({"error": "store not found"}, status_code=404)
        sb.table("venue_config").upsert({
            "store_id": store_id,
            "venue_name": params.get("venue_name", store.name),
            "stamp_goal": int(params.get("stamp_goal", 5)),
            "reward_text": params.get("reward_text", "a free drink or dessert"),
            "winback_days": int(params.get("winback_days", 5)),
            "code_expiry_days": int(params.get("code_expiry_days", 30)),
            "owner_phones": owner_phones,
            "qr_greeting": params.get("qr_greeting") or "",
        }).execute()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"status": "configured", "store_id": store_id})


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


@app.get("/admin/stores/{store_id}")
async def get_store(store_id: int) -> JSONResponse:
    from app.core.db import SessionLocal, Store, StoreMember, StoreTwilioNumber, POSConnection, RevenueConnection
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            return JSONResponse({"error": "not found"}, status_code=404)
        members = db.query(StoreMember).filter(StoreMember.store_id == store_id).all()
        twilio  = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == store_id).first()
        pos     = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
        rev     = db.query(RevenueConnection).filter(RevenueConnection.store_id == store_id).first()
    return JSONResponse({
        "id": store.id, "name": store.name, "chain_id": store.chain_id,
        "whatsapp_number": twilio.whatsapp_number if twilio else None,
        "members": [{"whatsapp": m.whatsapp, "role": m.role} for m in members],
        "pos_configured": pos is not None,
        "revenue_configured": rev is not None,
    })
