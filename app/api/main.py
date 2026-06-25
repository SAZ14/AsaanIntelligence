"""Asaan Intelligence API — Sugar Rush community agents."""

from __future__ import annotations

from app.api.deps import venue_name
from app.api.staff import router as staff_router
from app.api.webhooks import router as webhooks_router
from app.community.menu_context import build_enroll_qr_url
from app.community.store import load_venue_config
from app.services.messaging import twilio_whatsapp_digits
from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

from app.jobs.winback import run_winback
from app.jobs.leaderboard_broadcast import run_leaderboard_broadcast

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize scheduler
    scheduler = BackgroundScheduler()
    # Schedule winback daily at 10:00 AM
    scheduler.add_job(run_winback, 'cron', hour=10, minute=0)
    # Schedule leaderboard broadcast weekly on Sunday at 18:00
    scheduler.add_job(run_leaderboard_broadcast, 'cron', day_of_week='sun', hour=18, minute=0)
    
    scheduler.start()
    
    # Pre-load embedding model to prevent RAG cold-start latency
    import logging
    try:
        from app.community.store import preload_embedding_model
        preload_embedding_model()
        logging.info("Embedding model pre-loaded successfully.")
    except Exception as e:
        logging.error(f"Failed to pre-load embedding model: {e}")
        
    yield
    scheduler.shutdown()

app = FastAPI(title="Asaan Intelligence API", version="0.2.0", lifespan=lifespan)
app.include_router(webhooks_router)
app.include_router(staff_router)


@app.get("/health")
def health():
    config = load_venue_config()
    digits = twilio_whatsapp_digits("TWILIO_WHATSAPP_CUSTOMER_FROM")
    enroll_url = build_enroll_qr_url(digits, config.qr_greeting) if digits else ""
    return {
        "status": "ok",
        "venue": venue_name(),
        "enroll_qr_url": enroll_url,
        "customer_webhook": "/webhooks/twilio/customer",
        "merchant_webhook": "/webhooks/twilio/merchant",
        "receipt_api": "/staff/receipt",
    }
