"""FastAPI app exposing the Maître d' over a Twilio WhatsApp webhook.

Run with::

    uvicorn app.maitre_d.api:app --reload

Environment:
    MAITRE_D_DB              SQLite path (default: data/maitre_d.db)
    MAITRE_D_CONFIG          venue config JSON path (optional)
    MAITRE_D_USE_CLAUDE      "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
    MAITRE_D_PUBLIC_BASE_URL public https base used to verify Twilio signatures
                             behind a proxy, e.g. https://book.venue.com (optional)
    MAITRE_D_TICK_SECONDS    background maintenance interval (default 60; 0 disables)
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM  (outbound)
"""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Request, Response

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig
from app.maitre_d.store import Store
from app.maitre_d.whatsapp import (
    WhatsAppClient,
    parse_inbound,
    twiml_reply,
    validate_twilio_signature,
)


def _build_client():
    if os.environ.get("MAITRE_D_USE_CLAUDE") == "1":
        try:
            import anthropic
            return anthropic.Anthropic()
        except Exception:
            return None
    return None


def _request_url(request: Request) -> str:
    """The URL Twilio signed — overridable for proxied/HTTPS-terminated hosts."""
    base = os.environ.get("MAITRE_D_PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return str(request.url)


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

    def _dispatch(outbound: list[tuple[str, str]]) -> None:
        for phone, text in outbound:
            wa_client.send(phone, text)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "venue": config.name,
                "whatsapp_configured": wa_client.configured}

    @app.post("/webhook/whatsapp")
    async def whatsapp_webhook(request: Request) -> Response:
        form = dict(await request.form())

        # Reject forged requests when an auth token is configured.
        if not validate_twilio_signature(
            _request_url(request), form,
            request.headers.get("X-Twilio-Signature", ""),
            wa_client.auth_token,
        ):
            return Response(status_code=403, content="invalid signature")

        inbound = parse_inbound(form)
        if not inbound.body:
            return Response(content=twiml_reply(""), media_type="application/xml")

        reply = maitre_d.handle_message(
            inbound.from_phone, inbound.body, profile_name=inbound.profile_name
        )

        # Proactive messages to *other* guests (e.g. waitlist offers).
        _dispatch(reply.outbound)
        if reply.staff_alert:
            print(f"[maitre-d:staff-alert] {reply.staff_alert}")

        return Response(content=twiml_reply(reply.text), media_type="application/xml")

    @app.post("/webhook/payment")
    async def payment_webhook(request: Request) -> dict:
        """Deposit-gateway callback → confirm the held table once paid."""
        try:
            payload = await request.json()
        except Exception:
            payload = dict(await request.form())
        reply = maitre_d.handle_payment_webhook(payload)
        if reply is None:
            return {"ok": False, "confirmed": False}
        _dispatch(reply.outbound)
        return {"ok": True, "confirmed": True, "reservation_id": reply.reservation_id}

    # ── staff / door operations ──

    @app.post("/reservations/{reservation_id}/seat")
    def seat(reservation_id: str) -> dict:
        res = maitre_d.mark_seated(reservation_id)
        return _lifecycle_result(res, "seated")

    @app.post("/reservations/{reservation_id}/complete")
    def complete(reservation_id: str) -> dict:
        res = maitre_d.mark_completed(reservation_id)
        return _lifecycle_result(res, "completed")

    @app.post("/reservations/{reservation_id}/no_show")
    def no_show(reservation_id: str) -> dict:
        res = maitre_d.mark_no_show(reservation_id)
        return _lifecycle_result(res, "no_show")

    @app.post("/tasks/tick")
    def tick() -> dict:
        """Run one maintenance pass (offers, door sweep, reminders) and dispatch."""
        result = maitre_d.run_maintenance()
        _dispatch(result.outbound)
        for alert in result.staff_alerts:
            print(f"[maitre-d:staff-alert] {alert}")
        return {
            "offers_expired": result.offers_expired,
            "no_shows": result.no_shows,
            "completed": result.completed,
            "deposits_expired": result.deposits_expired,
            "reminders_sent": result.reminders_sent,
            "messages_sent": len(result.outbound),
        }

    @app.get("/reservations")
    def reservations(status: str | None = None) -> dict:
        rows = store.list_reservations(status)
        return {"count": len(rows),
                "reservations": [r.model_dump(mode="json") for r in rows]}

    @app.get("/waitlist")
    def waitlist(status: str | None = None) -> dict:
        rows = store.list_waitlist(status)
        return {"count": len(rows),
                "waitlist": [w.model_dump(mode="json") for w in rows]}

    # ── optional in-process scheduler ──

    @app.on_event("startup")
    async def _start_ticker() -> None:
        interval = int(os.environ.get("MAITRE_D_TICK_SECONDS", "60"))
        if interval <= 0:
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    result = await asyncio.to_thread(maitre_d.run_maintenance)
                    _dispatch(result.outbound)
                    for alert in result.staff_alerts:
                        print(f"[maitre-d:staff-alert] {alert}")
                except Exception as e:  # never let the ticker kill the app
                    print(f"[maitre-d:tick-error] {e}")

        app.state.ticker = asyncio.create_task(_loop())

    return app


def _lifecycle_result(res, target_status: str) -> dict:
    if res is None:
        return {"ok": False, "reason": f"cannot transition to {target_status}"}
    return {"ok": True, "reservation_id": res.reservation_id, "status": res.status}


app = create_app()
