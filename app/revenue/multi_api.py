"""Multi-café Revenue agent service.

A single WhatsApp bot ("AsaanPay Rev Agent") that every café owner has saved as a
contact. Each owner messages the same number; we identify their café by their own
phone and route to that café's private agent.

Run with::

    uvicorn app.revenue.multi_api:app --reload

Environment:
    REVENUE_TENANTS   path to the tenants registry JSON (default: data/revenue_tenants.example.json)
    REVENUE_USE_CLAUDE "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
    TWILIO_*          the single bot's credentials (account SID / token / WhatsApp number)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response

from app.revenue.tenants import TenantRegistry
from app.revenue.whatsapp import WhatsAppClient, parse_inbound, twiml_reply


def _build_client():
    if os.environ.get("REVENUE_USE_CLAUDE") == "1":
        try:
            import anthropic
            return anthropic.Anthropic()
        except Exception:
            return None
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="AsaanPay Revenue Advisor — multi-café")

    tenants_path = os.environ.get("REVENUE_TENANTS", "data/revenue_tenants.example.json")
    registry = TenantRegistry.load(tenants_path, client=_build_client())
    wa_client = WhatsAppClient()

    app.state.registry = registry
    app.state.wa_client = wa_client

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "cafes": len(registry.all_tenants()),
                "whatsapp_configured": wa_client.configured}

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook(request: Request) -> Response:
        form = dict(await request.form())
        inbound = parse_inbound(form)
        if not inbound.body:
            return Response(content=twiml_reply(""), media_type="application/xml")
        # Route by the owner's own number — that's how we know which café.
        reply = registry.handle(inbound.from_phone, inbound.body)
        return Response(content=twiml_reply(reply.text), media_type="application/xml")

    @app.post("/digests/{cadence}")
    def send_digests(cadence: str) -> dict:
        """Push scheduled digests across every café (wire to cron)."""
        replies = registry.generate_digests(cadence)
        sent = 0
        for r in replies:
            for phone, text in r.outbound:
                wa_client.send(phone, text)
                sent += 1
        return {"cadence": cadence, "cafes": len(registry.all_tenants()),
                "messages_sent": sent}

    @app.get("/cafes")
    def cafes() -> dict:
        return {"count": len(registry.all_tenants()),
                "cafes": [{"cafe_id": t.cafe_id, "name": t.name,
                           "owners": len(t.owner_phones)}
                          for t in registry.all_tenants()]}

    return app


app = create_app()
