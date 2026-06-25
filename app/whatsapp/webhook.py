"""FastAPI WhatsApp webhook + admin endpoints.

Production: DB-driven multi-store routing (same pattern as scout agent).
  - Looks up stores by sender WhatsApp number via store_members table
  - Session management via user_sessions table
  - POS config fetched from pos_connections table

Tests: pass a pre-built IntegrityWhatsAppService to create_app() — DB is
  bypassed entirely and the injected service handles routing.
"""
from __future__ import annotations

import logging
import os
from typing import Annotated
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from app.agents.integrity_agent import run_integrity_agent
from app.pos import RestaurantConfig, build_connector
from app.report.pdf import build_audit_pdf
from app.whatsapp.service import IntegrityWhatsAppService

logger = logging.getLogger(__name__)

REPORT_COMMANDS = {"report", "pdf", "document"}

try:
    from twilio.request_validator import RequestValidator
except Exception:
    RequestValidator = None  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _twiml(message: str, media_urls: list[str] | None = None) -> str:
    media = "".join(f"<Media>{escape(u)}</Media>" for u in (media_urls or []))
    body = escape(message)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message><Body>{body}</Body>{media}</Message></Response>"
    )


def _twiml_resp(message: str, media_urls: list[str] | None = None) -> Response:
    return Response(content=_twiml(message, media_urls), media_type="application/xml")


def _verify(request: Request, params: dict, url: str) -> bool:
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token or RequestValidator is None:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    return RequestValidator(token).validate(url, params, signature)


def _store_menu(stores) -> str:
    lines = ["Which restaurant would you like to check on?\n"]
    for i, s in enumerate(stores, 1):
        lines.append(f"{i}) {s.name}" + (f" — {s.location}" if s.location else ""))
    lines.append("\nReply with the number.")
    return "\n".join(lines)


def _parse_store_selection(text: str, stores) -> object | None:
    text = text.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(stores):
            return stores[idx]
    return None


# Per-store service instances (cached for the process lifetime so reports are cached)
_store_services: dict[int, IntegrityWhatsAppService] = {}


def _get_or_create_service(store_id: int) -> IntegrityWhatsAppService | None:
    if store_id in _store_services:
        return _store_services[store_id]
    from app.venues import get_restaurant_config
    config = get_restaurant_config(store_id)
    if config is None:
        return None
    venue_key = str(store_id)
    svc = IntegrityWhatsAppService(
        restaurants={venue_key: config},
        owner_map={},
        default_venue=venue_key,
    )
    _store_services[store_id] = svc
    return svc


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(service: IntegrityWhatsAppService | None = None) -> FastAPI:
    """
    service=None  → production mode: DB-driven multi-store routing
    service=<svc> → test/demo mode: injected service, bypass DB
    """
    from app.db import init_db

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            init_db()
            logger.info("DB initialised")
        yield

    app = FastAPI(title="AsaanPay Integrity Agent — WhatsApp", lifespan=lifespan)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # ── Health ──

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ── PDF report endpoint ──

    @app.get("/report/{venue}.pdf")
    def report_pdf(venue: str) -> Response:
        if service is not None:
            # Test/injected mode: use venue key string
            if venue not in service.restaurants:
                return Response(status_code=404, content="unknown venue")
            config = service.restaurants[venue]
            data = build_connector(config).fetch()
            r = run_integrity_agent(
                data.orders, data.menu, data.staff,
                venue_name=config.venue_name, use_llm=False,
            )
        else:
            # Production mode: venue is the store_id
            try:
                store_id = int(venue)
            except ValueError:
                return Response(status_code=404, content="unknown venue")
            from app.venues import get_restaurant_config
            config = get_restaurant_config(store_id)
            if config is None:
                return Response(status_code=404, content="no POS configured for this store")
            data = build_connector(config).fetch()
            r = run_integrity_agent(
                data.orders, data.menu, data.staff,
                venue_name=config.venue_name, use_llm=False,
            )

        pdf = build_audit_pdf(
            r.integrity, r.reconciliation,
            venue_name=r.venue_name, summary=r.executive_summary,
        )
        return Response(
            content=pdf, media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="audit_{venue}.pdf"'},
        )

    # ── WhatsApp webhook ──

    @app.post("/whatsapp")
    async def whatsapp(request: Request) -> Response:
        raw = (await request.body()).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(raw).items()}

        from_number = params.get("From", "")
        body = params.get("Body", "")

        # ── Test/injected mode — bypass signature check ──
        if service is not None:
            reply = service.handle_message(from_number, body)
            media: list[str] = []
            first = (body or "").strip().lower().split()
            if first and first[0] in REPORT_COMMANDS:
                venue = service.resolve_venue(from_number)
                if venue and venue in service.restaurants:
                    media = [f"{str(request.base_url).rstrip('/')}/report/{venue}.pdf"]
            return _twiml_resp(reply, media or None)

        # ── Production: verify Twilio signature then DB-driven routing ──
        if not _verify(request, params, str(request.url)):
            return Response(status_code=403, content="invalid signature")

        from app.db import (
            get_stores_for_number, get_user_session, set_user_session,
        )

        logger.info("Webhook: message=%r from=%s", (body or "")[:80], from_number)

        stores = get_stores_for_number(from_number)
        if not stores:
            return _twiml_resp("You're not registered. Ask your admin to add your number.")

        first_word = (body or "").strip().lower().split()
        is_switch = first_word and first_word[0] in ("switch", "change", "swap", "different")

        if is_switch:
            if len(stores) == 1:
                return _twiml_resp(f"You only have one restaurant registered: {stores[0].name}.")
            set_user_session(from_number, None)
            return _twiml_resp(_store_menu(stores))

        session = get_user_session(from_number)

        # Awaiting store selection
        if session is not None and session.store_id is None:
            selected = _parse_store_selection(body or "", stores)
            if selected:
                set_user_session(from_number, selected.id)
                store_id = selected.id
            else:
                return _twiml_resp(_store_menu(stores))
        elif len(stores) == 1:
            store_id = stores[0].id
        elif session and session.store_id:
            store_id = session.store_id
        else:
            set_user_session(from_number, None)
            return _twiml_resp(_store_menu(stores))

        svc = _get_or_create_service(store_id)
        if svc is None:
            return _twiml_resp(
                "This restaurant doesn't have a POS connection configured yet. "
                "Ask your admin to set it up via /admin/stores/{id}/pos."
            )

        reply = svc.handle_message(from_number, body or "")

        media_urls: list[str] = []
        cmd = (body or "").strip().lower().split()
        if cmd and cmd[0] in REPORT_COMMANDS:
            media_urls = [f"{str(request.base_url).rstrip('/')}/report/{store_id}.pdf"]

        # Persist audit stats to DB after a successful report
        try:
            cached = svc._cache.get(str(store_id))
            if cached:
                r = cached.report
                rec = r.reconciliation
                integ = r.integrity
                from app.db import save_integrity_run, save_integrity_report
                run_id = save_integrity_run(
                    store_id=store_id,
                    period_days=r.period_days,
                    net_sales=rec.net_sales,
                    gross_profit=rec.gross_profit,
                    gross_margin=rec.gross_margin,
                    estimated_leakage=integ.estimated_leakage_period,
                    finding_count=len(r.findings),
                    llm_used=r.llm_used,
                )
                save_integrity_report(store_id, run_id, cmd[0] if cmd else "message", reply)
        except Exception as exc:
            logger.warning("Failed to persist audit run: %s", exc)

        return _twiml_resp(reply, media_urls or None)

    # ── Admin endpoints ──

    @app.post("/admin/stores")
    async def create_store(request: Request) -> Response:
        from app.db import SessionLocal, Store
        raw = (await request.body()).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        name = params.get("name", "").strip()
        if not name:
            return Response(status_code=400, content="name is required")
        with SessionLocal() as db:
            store = Store(
                name=name,
                location=params.get("location") or None,
                category=params.get("category") or None,
                instagram_handle=params.get("instagram_handle") or None,
            )
            db.add(store)
            db.commit()
            store_id_out = store.id
            store_name_out = store.name
        return JSONResponse({"store_id": store_id_out, "name": store_name_out})

    @app.post("/admin/stores/{store_id}/members")
    async def add_member(store_id: int, request: Request) -> Response:
        from app.db import SessionLocal, Store, StoreMember
        raw = (await request.body()).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        whatsapp = params.get("whatsapp", "").strip()
        role = params.get("role", "owner")
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            if not store:
                return Response(status_code=404, content="store not found")
            existing = db.query(StoreMember).filter(
                StoreMember.store_id == store_id,
                StoreMember.whatsapp == whatsapp,
            ).first()
            if existing:
                return JSONResponse({"status": "already_exists"})
            db.add(StoreMember(store_id=store_id, whatsapp=whatsapp, role=role))
            db.commit()
        return JSONResponse({"status": "added", "store_id": store_id, "whatsapp": whatsapp})

    @app.post("/admin/stores/{store_id}/pos")
    async def configure_pos(store_id: int, request: Request) -> Response:
        """Register or update the POS connection for a store."""
        from app.db import SessionLocal, Store, POSConnection
        import json
        raw = (await request.body()).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        pos_type = params.get("pos_type", "csv")
        mapping = params.get("mapping", "cafe_generic")
        currency = params.get("currency", "PKR")
        timezone = params.get("timezone", "Asia/Karachi")
        try:
            config_json = json.loads(params.get("config", "{}"))
        except Exception:
            return Response(status_code=400, content="config must be valid JSON")

        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            if not store:
                return Response(status_code=404, content="store not found")
            existing = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
            if existing:
                existing.pos_type = pos_type
                existing.config = config_json
                existing.mapping = mapping
                existing.currency = currency
                existing.timezone = timezone
            else:
                db.add(POSConnection(
                    store_id=store_id,
                    pos_type=pos_type,
                    config=config_json,
                    mapping=mapping,
                    currency=currency,
                    timezone=timezone,
                ))
            db.commit()
        # Invalidate cached service so new config is picked up
        _store_services.pop(store_id, None)
        return JSONResponse({"status": "configured", "store_id": store_id, "pos_type": pos_type})

    @app.get("/admin/stores")
    async def list_stores() -> Response:
        from app.db import SessionLocal, Store, POSConnection
        with SessionLocal() as db:
            stores = db.query(Store).all()
            result = []
            for s in stores:
                pos = db.query(POSConnection).filter(POSConnection.store_id == s.id).first()
                result.append({
                    "id": s.id,
                    "name": s.name,
                    "category": s.category,
                    "pos_configured": pos is not None,
                    "pos_type": pos.pos_type if pos else None,
                })
        return JSONResponse(result)

    return app


app = create_app()
