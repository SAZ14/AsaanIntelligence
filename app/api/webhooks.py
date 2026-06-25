"""Twilio webhooks — customer and merchant WhatsApp agents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from app.api.deps import validate_twilio_request

from app.agents.community_customer import process_customer_reply
from app.agents.community_merchant import process_merchant_reply

router = APIRouter(
    prefix="/webhooks/twilio",
    tags=["webhooks"],
    dependencies=[Depends(validate_twilio_request)],
)


def _empty_twiml() -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


@router.post("/customer")
async def twilio_customer_inbound(
    From: str = Form(...),
    Body: str = Form(default=""),
):
    if not From:
        raise HTTPException(status_code=400, detail="Missing From")
    try:
        process_customer_reply(From, Body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _empty_twiml()


@router.post("/merchant")
async def twilio_merchant_inbound(
    From: str = Form(...),
    Body: str = Form(default=""),
    NumMedia: int = Form(default=0),
    MediaUrl0: str = Form(default=None),
    MediaContentType0: str = Form(default=None),
):
    if not From:
        raise HTTPException(status_code=400, detail="Missing From")
    try:
        if NumMedia > 0 and MediaContentType0 == "application/pdf":
            from app.ingest.pdf_processor import process_and_store_pdf
            # Synchronously process for simplicity, ideally would be a background task
            num_chunks = process_and_store_pdf(MediaUrl0, "Uploaded_Document.pdf")
            reply_text = f"Successfully learned {num_chunks} segments from your PDF!"
            from app.agents.community_merchant import send_whatsapp_text
            send_whatsapp_text(From, reply_text, from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return _empty_twiml()
            
        process_merchant_reply(From, Body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _empty_twiml()
