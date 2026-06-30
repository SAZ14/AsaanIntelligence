"""FastAPI inbound webhook for OpenWA (demo transport).

Replaces webhook.py for the demo/openwa branch. OpenWA POSTs JSON events here;
we call IntegrityWhatsAppService and reply via openwa_client (separate async
POST — OpenWA does not use synchronous TwiML-style replies).

Run:
    uvicorn app.whatsapp.openwa_webhook:app --host 0.0.0.0 --port 8000

Environment variables:
    OPENWA_BASE_URL    e.g. "http://localhost:2785/api"
    OPENWA_SESSION_ID  e.g. "default"
    OPENWA_API_KEY     API key from OpenWA dashboard
    OPENWA_SELF_URL    e.g. "http://192.168.1.10:8000" — triggers auto webhook
                       registration on startup so you don't need to curl it.
"""

from __future__ import annotations

import json
import logging
import os
from urllib import request as urllib_request

from fastapi import FastAPI, Request, Response

from app.agents.integrity_agent import run_integrity_agent
from app.pos import build_connector
from app.report.pdf import build_audit_pdf
from app.whatsapp.openwa_client import send_document, send_text
from app.whatsapp.service import IntegrityWhatsAppService

log = logging.getLogger(__name__)

REPORT_COMMANDS = {"report", "pdf", "document"}


def _register_webhook(base_url: str, session_id: str, api_key: str, self_url: str) -> None:
    url = f"{base_url}/sessions/{session_id}/webhooks"
    payload = json.dumps({
        "url": f"{self_url.rstrip('/')}/openwa",
        "events": ["message.received"],
    }).encode()
    req = urllib_request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "X-Api-Key": api_key},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:  # noqa: S310
            log.info("Webhook registered: %s", resp.read().decode()[:200])
    except Exception as exc:
        log.warning("Webhook registration failed (will work if already set): %s", exc)


def create_app(service: IntegrityWhatsAppService | None = None) -> FastAPI:
    svc = service or IntegrityWhatsAppService()
    app = FastAPI(title="AsaanPay Integrity Agent — WhatsApp (OpenWA)")

    @app.on_event("startup")
    def _startup() -> None:
        self_url = os.environ.get("OPENWA_SELF_URL", "")
        if not self_url:
            log.info("OPENWA_SELF_URL not set — skipping auto webhook registration.")
            return
        base = os.environ.get("OPENWA_BASE_URL", "http://localhost:2785/api").rstrip("/")
        session = os.environ.get("OPENWA_SESSION_ID", "default")
        key = os.environ.get("OPENWA_API_KEY", "")
        _register_webhook(base, session, key, self_url)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/report/{venue}.pdf")
    def report_pdf(venue: str) -> Response:
        if venue not in svc.restaurants:
            return Response(status_code=404, content="unknown venue")
        config = svc.restaurants[venue]
        data = build_connector(config).fetch()
        r = run_integrity_agent(
            data.orders, data.menu, data.staff,
            venue_name=config.venue_name, use_llm=False,
        )
        pdf = build_audit_pdf(
            r.integrity, r.reconciliation,
            venue_name=config.venue_name, summary=r.executive_summary,
        )
        return Response(
            content=pdf, media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="audit_{venue}.pdf"'},
        )

    @app.post("/openwa")
    async def openwa_inbound(request: Request) -> dict:
        body = await request.json()

        event = body.get("event", "")
        if event != "message.received":
            return {"status": "ignored", "event": event}

        data = body.get("data", {})
        if data.get("fromMe", False):
            return {"status": "ignored", "reason": "fromMe"}

        from_number = data.get("from", "")
        message_body = data.get("body", "")

        if not from_number or not message_body:
            return {"status": "ignored", "reason": "empty"}

        reply = svc.handle_message(from_number, message_body)

        first_word = message_body.strip().lower().split()[0] if message_body.strip() else ""
        if first_word in REPORT_COMMANDS:
            venue = svc.resolve_venue(from_number)
            if venue and venue in svc.restaurants:
                config = svc.restaurants[venue]
                pos_data = build_connector(config).fetch()
                r = run_integrity_agent(
                    pos_data.orders, pos_data.menu, pos_data.staff,
                    venue_name=config.venue_name, use_llm=False,
                )
                pdf_bytes = build_audit_pdf(
                    r.integrity, r.reconciliation,
                    venue_name=config.venue_name, summary=r.executive_summary,
                )
                try:
                    send_document(
                        from_number,
                        pdf_bytes,
                        filename=f"audit_{venue}.pdf",
                        caption=reply,
                    )
                except Exception as exc:
                    log.error("send_document failed: %s", exc)
                return {"status": "ok", "type": "document"}

        try:
            send_text(from_number, reply)
        except Exception as exc:
            log.error("send_text failed: %s", exc)
        return {"status": "ok", "type": "text"}

    return app


app = create_app()
