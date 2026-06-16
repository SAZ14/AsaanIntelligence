"""Revenue agent — POS-driven pricing, dead-window and campaign advisor.

Tracks what sells, finds products with pricing power, detects low-demand windows
and recommends brand-safe micro-campaigns to fill them — and answers the owner's
questions over WhatsApp (with scheduled daily/weekly/monthly digests).

This package is fully self-contained and modifies no other agent. It *reuses*
(read-only) the existing POS loader and analysis:
    app.ingest.loader.load_dataset
    app.analysis.retention.analyze_operations / analyze_retention

Layout:
    config.py     campaign catalogue, segment + redemption assumptions, pricing knobs
    models.py     Pydantic models for owner subscriptions + campaign log (SQLite)
    datasource.py load POS data + filter to a reporting period
    pricing.py    transparent "pricing power" scoring → which items can take a raise
    segments.py   POS-derived customer segments + redemption-rate estimates
    analytics.py  product performance, dead windows, campaign recommendations, digests
    store.py      SQLite: owner digest subscriptions + campaign log
    nlu.py        Claude NLU for the owner's free-text questions (+ deterministic fallback)
    agent.py      the advisor engine (code makes the numbers; Claude only parses)
    whatsapp.py   Twilio WhatsApp inbound parse + outbound client
    api.py        FastAPI webhook + report endpoints
"""

from app.revenue.agent import RevenueAgent, RevenueReply, run_revenue_agent

__all__ = ["RevenueAgent", "RevenueReply", "run_revenue_agent"]
