"""FastAPI app exposing the Maître d' over a Twilio WhatsApp webhook.

Run with::

    uvicorn app.maitre_d.api:app --reload

Environment:
    MAITRE_D_DB           SQLite path (default: data/maitre_d.db)
    MAITRE_D_CONFIG       venue config JSON path (optional)
    MAITRE_D_USE_CLAUDE   "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM  (outbound)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig
from app.maitre_d.store import Store
from app.maitre_d.whatsapp import (
    WhatsAppClient,
    parse_inbound,
    twiml_reply,
)


def _build_client():
    if os.environ.get("MAITRE_D_USE_CLAUDE") == "1":
        try:
            import anthropic
            return anthropic.Anthropic()
        except Exception:
            return None
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="Maître d' — reservations & the door")

    db_path = os.environ.get("MAITRE_D_DB", "data/maitre_d.db")
    config_path = os.environ.get("MAITRE_D_CONFIG")

    store = Store(db_path)
    config = VenueConfig.load(config_path)
    maitre_d = MaitreD(store=store, config=config, client=_build_client())
    wa_client = WhatsAppClient()

    app.state.store = store
    app.state.maitre_d = maitre_d
    app.state.wa_client = wa_client

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "venue": config.name, "whatsapp_configured": wa_client.configured}

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook(request: Request) -> Response:
        form = dict(await request.form())
        inbound = parse_inbound(form)
        if not inbound.body:
            return Response(content=twiml_reply(""), media_type="application/xml")

        reply = maitre_d.handle_message(
            inbound.from_phone, inbound.body, profile_name=inbound.profile_name
        )

        # Proactive messages to *other* guests (e.g. waitlist offers).
        for phone, text in reply.outbound:
            wa_client.send(phone, text)

        if reply.staff_alert:
            print(f"[maitre-d:staff-alert] {reply.staff_alert}")

        return Response(content=twiml_reply(reply.text), media_type="application/xml")

    @app.get("/reservations")
    def reservations(status: str | None = None) -> dict:
        rows = store.list_reservations(status)
        return {"count": len(rows), "reservations": [r.model_dump(mode="json") for r in rows]}

    @app.get("/waitlist")
    def waitlist(status: str | None = None) -> dict:
        rows = store.list_waitlist(status)
        return {"count": len(rows), "waitlist": [w.model_dump(mode="json") for w in rows]}

    return app


app = create_app()
