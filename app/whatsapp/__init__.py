"""WhatsApp (Twilio) channel for the integrity agent.

Lets a restaurant owner message the agent and ask about leakage, profit,
revenue, and specific findings. ``service.IntegrityWhatsAppService`` is the
transport-agnostic brain (text in -> text out); ``webhook`` exposes it over a
Twilio webhook; ``twilio_client`` sends proactive alerts outbound.
"""

from app.whatsapp.service import IntegrityWhatsAppService

__all__ = ["IntegrityWhatsAppService"]
