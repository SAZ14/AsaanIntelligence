"""FastAPI app exposing the Revenue agent over a Twilio WhatsApp webhook.

Run with::

    uvicorn app.revenue.api:app --reload

Environment:
    REVENUE_DB            SQLite path (default: data/revenue.db)
    REVENUE_CONFIG        revenue config JSON path (optional)
    REVENUE_DATA_DIR      POS data directory (default: repo data/)
    REVENUE_USE_CLAUDE    "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
    TWILIO_*              outbound digest credentials
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response

from app.revenue.agent import RevenueAgent, cadence_to_period
from app.revenue.config import RevenueConfig
from app.revenue.store import Store
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
    app = FastAPI(title="Revenue agent — POS pricing & campaign advisor")

    store = Store(os.environ.get("REVENUE_DB", "data/revenue.db"))
    config = RevenueConfig.load(os.environ.get("REVENUE_CONFIG"))
    agent = RevenueAgent(
        store=store, config=config, client=_build_client(),
        data_dir=os.environ.get("REVENUE_DATA_DIR"),
    )
    wa_client = WhatsAppClient()

    app.state.store = store
    app.state.agent = agent
    app.state.wa_client = wa_client

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "venue": config.venue_name,
                "orders_loaded": len(agent.orders),
                "whatsapp_configured": wa_client.configured}

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook(request: Request) -> Response:
        form = dict(await request.form())
        inbound = parse_inbound(form)
        if not inbound.body:
            return Response(content=twiml_reply(""), media_type="application/xml")
        reply = agent.handle_message(inbound.from_phone, inbound.body)
        return Response(content=twiml_reply(reply.text), media_type="application/xml")

    @app.post("/digests/{cadence}")
    def send_digests(cadence: str) -> dict:
        """Trigger scheduled digests for a cadence (daily|weekly|monthly).

        Wire this to a cron / scheduler. Sends to every active subscriber.
        """
        replies = agent.generate_due_digests(cadence)
        sent = 0
        for r in replies:
            for phone, text in r.outbound:
                wa_client.send(phone, text)
                sent += 1
        return {"cadence": cadence, "period": cadence_to_period(cadence),
                "subscribers": len(replies), "messages_sent": sent}

    @app.get("/report")
    def report(period: str = "week") -> dict:
        digest = agent.build_owner_digest(period)
        return {
            "period": digest.period_label,
            "revenue": digest.performance.total_revenue,
            "orders": digest.performance.order_count,
            "top_sellers": [
                {"name": s.name, "units": s.units, "revenue": s.revenue}
                for s in digest.performance.top_sellers
            ],
            "pricing": [
                {"name": r.name, "current": r.current_price,
                 "suggested": r.suggested_price, "monthly_uplift": r.est_monthly_uplift}
                for r in digest.pricing[:5]
            ],
            "dead_windows": [w.__dict__ for w in digest.dead_windows],
            "campaigns": [
                {"window": c.window_desc, "campaign": c.campaign_name,
                 "audience": c.audience_size, "expected_redemptions": c.expected_redemptions,
                 "added_revenue": c.est_added_revenue}
                for c in digest.campaigns
            ],
        }

    @app.get("/campaigns")
    def campaigns() -> dict:
        rows = store.list_campaigns()
        return {"count": len(rows), "campaigns": [c.model_dump(mode="json") for c in rows]}

    return app


app = create_app()
