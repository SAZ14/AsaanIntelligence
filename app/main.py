from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import TWILIO_VALIDATE_SIGNATURE, TWILIO_AUTH_TOKEN
from app.db import SessionLocal, Report, Run, init_db
from app import pipeline, send
from app.analysis import classify_intent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

VALID_COMMANDS = {"scout", "alerts", "competitors", "campaigns", "opportunities", "pricing", "content", "help"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("DB initialised")
    yield


app = FastAPI(title="Sugar Rush Scout Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_twilio_signature(request: Request, params: dict) -> bool:
    if not TWILIO_VALIDATE_SIGNATURE:
        return True
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        url = str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")
        return validator.validate(url, params, signature)
    except Exception as exc:
        logger.warning("Signature validation error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    Body: Annotated[str, Form()] = "",
    From: Annotated[str, Form()] = "",
):
    """Twilio WhatsApp inbound webhook. Acks immediately, processes in background."""
    if TWILIO_VALIDATE_SIGNATURE and not _validate_twilio_signature(
        request, {"Body": Body, "From": From}
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    message_text = Body.strip()
    sender = From
    logger.info("Webhook: message=%r from=%s", message_text[:80], sender)

    # Immediate TwiML ack
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>Got it! Analysing competitors... reply coming in ~30–60s.</Message>
</Response>"""

    background_tasks.add_task(_run_and_reply, message_text, sender)
    return PlainTextResponse(content=twiml, media_type="text/xml")


def _run_and_reply(message_text: str, sender: str) -> None:
    try:
        # Classify natural language → pipeline command
        command = classify_intent(message_text)
        logger.info("Classified %r → %s", message_text[:60], command)
        report = pipeline.run(command, user_message=message_text)
        send.send_whatsapp(sender, report)
    except Exception as exc:
        logger.error("Background pipeline failed: %s", exc)
        try:
            send.send_whatsapp(
                sender,
                f"Sorry, hit an error: {type(exc).__name__}. Please try again shortly.",
            )
        except Exception:
            pass


@app.post("/scout")
async def scout_endpoint():
    """Shorthand for POST /run/scout."""
    return await run_command("scout")


@app.post("/run/{command}")
async def run_command(command: str):
    """
    Run any command via HTTP — no WhatsApp needed.
    Valid commands: scout, alerts, competitors, campaigns, opportunities, pricing, content, help
    Returns JSON: {report, command, run_id, findings_count}
    """
    command = command.lower().strip()
    if command not in VALID_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command '{command}'. Valid: {sorted(VALID_COMMANDS)}",
        )
    try:
        report = pipeline.run(command)
        with SessionLocal() as db:
            latest_run = (
                db.query(Run)
                .filter(Run.status.in_(["ok", "partial"]))
                .order_by(Run.finished_at.desc())
                .first()
            )
            run_id = latest_run.id if latest_run else None
            finding_count = latest_run.finding_count if latest_run else 0
        return JSONResponse({
            "report": report,
            "command": command,
            "run_id": run_id,
            "findings_count": finding_count,
        })
    except Exception as exc:
        logger.error("/run/%s error: %s", command, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/report/latest")
async def latest_report(command: str = "scout"):
    """Return the most recent stored report for a given command."""
    with SessionLocal() as db:
        report = (
            db.query(Report)
            .filter(Report.command == command)
            .order_by(Report.created_at.desc())
            .first()
        )
    if not report:
        raise HTTPException(status_code=404, detail=f"No report found for command '{command}'")
    return JSONResponse({
        "command": report.command,
        "report_text": report.report_text,
        "created_at": str(report.created_at),
        "run_id": report.run_id,
    })
