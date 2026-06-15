"""WhatsApp Cloud API integration for the Reputation agent.

Modules:
    config    — env-driven WhatsAppConfig (+ DRY_RUN flag)
    notifier  — build/send the interactive review-alert message and plain text
    webhook   — FastAPI router: Meta verification handshake + inbound parsing
"""
