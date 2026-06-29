from __future__ import annotations
import logging

from app.agents.scout.config import TWILIO_WHATSAPP_FROM

logger = logging.getLogger(__name__)


def send_whatsapp(to: str, text: str) -> None:
    from app.core.twilio_send import send_whatsapp as _send
    _send(to=to, body=text, from_=TWILIO_WHATSAPP_FROM)

