"""FastAPI Twilio WhatsApp webhook.

Twilio POSTs each inbound WhatsApp message here (form-encoded: ``From``,
``Body``, …). We hand it to :class:`IntegrityWhatsAppService` and reply with
TwiML, so answering the owner needs no outbound credentials.

Run it::

    uvicorn app.whatsapp.webhook:app --host 0.0.0.0 --port 8000

then point your Twilio WhatsApp sandbox/number's "When a message comes in"
webhook at  https://<host>/whatsapp .

If ``TWILIO_AUTH_TOKEN`` is set (and the ``twilio`` SDK is installed), inbound
requests are signature-verified; otherwise verification is skipped (dev mode).
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from app.agents.integrity_agent import run_integrity_agent
from app.pos import build_connector
from app.report.pdf import build_audit_pdf
from app.whatsapp.service import IntegrityWhatsAppService

REPORT_COMMANDS = {"report", "pdf", "document"}

try:  # optional: only needed for signature verification
    from twilio.request_validator import RequestValidator
except Exception:  # pragma: no cover
    RequestValidator = None  # type: ignore


def _twiml(message: str, media_urls: list[str] | None = None) -> str:
    media = "".join(f"<Media>{escape(u)}</Media>" for u in (media_urls or []))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message><Body>{escape(message)}</Body>{media}</Message></Response>"
    )


def _verify(request: Request, params: dict, url: str) -> bool:
    """Validate Twilio's X-Twilio-Signature. True if valid or verification off."""
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token or RequestValidator is None:
        return True  # dev mode — verification disabled
    signature = request.headers.get("X-Twilio-Signature", "")
    return RequestValidator(token).validate(url, params, signature)


def create_app(service: IntegrityWhatsAppService | None = None) -> FastAPI:
    svc = service or IntegrityWhatsAppService()
    app = FastAPI(title="AsaanPay Integrity Agent — WhatsApp")

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

    @app.post("/whatsapp")
    async def whatsapp(request: Request) -> Response:
        # Twilio posts application/x-www-form-urlencoded; parse it from the raw
        # body so we don't depend on python-multipart.
        raw = (await request.body()).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        if not _verify(request, params, str(request.url)):
            return Response(status_code=403, content="invalid signature")

        from_number = params.get("From", "")
        body = params.get("Body", "")
        reply = svc.handle_message(from_number, body)

        # If the owner asked for the PDF, attach it as media. Twilio fetches the
        # URL, so this host must be publicly reachable.
        media: list[str] = []
        first = body.strip().lower().split()
        if first and first[0] in REPORT_COMMANDS:
            venue = svc.resolve_venue(from_number)
            if venue and venue in svc.restaurants:
                media = [f"{str(request.base_url).rstrip('/')}/report/{venue}.pdf"]

        return Response(content=_twiml(reply, media), media_type="application/xml")

    return app


app = create_app()
