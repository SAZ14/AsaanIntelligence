"""Twilio webhooks — customer and merchant WhatsApp agents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Response, BackgroundTasks
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


def _process_pdf_background(media_url: str, from_phone: str) -> None:
    try:
        from app.ingest.pdf_processor import process_and_store_pdf
        num_chunks = process_and_store_pdf(media_url, "Uploaded_Document.pdf")
        reply_text = f"Successfully learned {num_chunks} segments from your PDF!"
    except Exception as e:
        reply_text = f"Failed to process PDF: {str(e)}"
        
    try:
        from app.agents.community_merchant import send_whatsapp_text
        send_whatsapp_text(from_phone, reply_text, from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
    except Exception:
        pass


@router.post("/customer")
async def twilio_customer_inbound(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(default=""),
    MessageSid: str = Form(default=None),
):
    if not From:
        raise HTTPException(status_code=400, detail="Missing From")
    try:
        if MessageSid:
            from app.services.messaging import send_whatsapp_typing_indicator
            background_tasks.add_task(send_whatsapp_typing_indicator, MessageSid)
        process_customer_reply(From, Body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _empty_twiml()


@router.post("/merchant")
async def twilio_merchant_inbound(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(default=""),
    NumMedia: int = Form(default=0),
    MediaUrl0: str = Form(default=None),
    MediaContentType0: str = Form(default=None),
    MessageSid: str = Form(default=None),
):
    if not From:
        raise HTTPException(status_code=400, detail="Missing From")
    try:
        if MessageSid:
            from app.services.messaging import send_whatsapp_typing_indicator
            background_tasks.add_task(send_whatsapp_typing_indicator, MessageSid)
            
        if NumMedia > 0 and MediaContentType0 == "application/pdf":
            background_tasks.add_task(_process_pdf_background, MediaUrl0, From)
            return _empty_twiml()
            
        process_merchant_reply(From, Body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _empty_twiml()
