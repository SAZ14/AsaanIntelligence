"""Customer-facing WhatsApp handler.

Each restaurant has its own Twilio number in store_twilio_numbers.
When a customer texts that number, the To field tells us which store it is.
The customer agent is then invoked with that store_id.
"""
from __future__ import annotations
import logging

from app.core.db import get_store_by_twilio_number

logger = logging.getLogger(__name__)


def handle_customer_message(to_number: str, from_phone: str, body: str) -> str:
    """Route a customer message to the correct store's agent. Returns reply text."""
    store = get_store_by_twilio_number(to_number)
    if store is None:
        return (
            "Hi! This number is not currently active. "
            "Please scan the QR code at the restaurant counter."
        )
    store_id = store.id
    try:
        from app.agents.customer.agents.community_customer import handle_customer_message as _handle
        reply = _handle(from_phone, body, store_id=store_id)
        return reply.body
    except Exception as e:
        logger.error("Customer agent error store=%d: %s", store_id, e)
        return "Something went wrong — please try again."
