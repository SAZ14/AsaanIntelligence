"""Customer-facing handler — store_id already resolved by the unified webhook."""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def handle_customer_for_store(from_phone: str, body: str, store_id: int) -> str:
    """Invoke the customer agent for a known store. Returns reply text."""
    try:
        from app.agents.customer.agents.community_customer import handle_customer_message
        reply = handle_customer_message(from_phone, body, store_id=store_id)
        return reply.body
    except Exception as e:
        logger.error("Customer agent error store=%d: %s", store_id, e)
        return "Something went wrong — please try again."
