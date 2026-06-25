from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import TWILIO_VALIDATE_SIGNATURE, TWILIO_AUTH_TOKEN
from app.db import (
    SessionLocal, Store, StoreMember, Report, Run, init_db,
    get_stores_for_number, get_user_session, set_user_session,
)
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


def _twiml(message: str) -> PlainTextResponse:
    escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
    return PlainTextResponse(content=xml, media_type="text/xml")


def _twiml_empty() -> PlainTextResponse:
    return PlainTextResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="text/xml",
    )


def _store_menu(stores: list[Store]) -> str:
    lines = ["Which restaurant would you like to check on?\n"]
    for i, s in enumerate(stores, 1):
        lines.append(f"{i}) {s.name}" + (f" — {s.location}" if s.location else ""))
    lines.append("\nReply with the number.")
    return "\n".join(lines)


def _parse_store_selection(text: str, stores: list[Store]) -> Store | None:
    text = text.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(stores):
            return stores[idx]
    return None


def _strip_freshness(report: str) -> str:
    lines = report.split("\n")
    filtered = [l for l in lines if not l.startswith("Data:")]
    return "\n".join(filtered).lstrip("\n")


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
    """Twilio WhatsApp inbound webhook."""
    if TWILIO_VALIDATE_SIGNATURE and not _validate_twilio_signature(
        request, {"Body": Body, "From": From}
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    message_text = Body.strip()
    sender = From
    logger.info("Webhook: message=%r from=%s", message_text[:80], sender)

    # 1. Look up which stores this number has access to
    stores = get_stores_for_number(sender)
    if not stores:
        return _twiml("You're not registered. Ask your admin to add your number.")

    # 2. Check intent early to detect "switch" before any session logic
    intent = classify_intent(message_text)

    if intent == "switch":
        if len(stores) == 1:
            return _twiml(f"You only have one restaurant registered: {stores[0].name}.")
        set_user_session(sender, None)
        return _twiml(_store_menu(stores))

    # 3. Check if we're awaiting store selection from a previous message
    session = get_user_session(sender)
    if session is not None and session.store_id is None:
        selected = _parse_store_selection(message_text, stores)
        if selected:
            set_user_session(sender, selected.store_id if hasattr(selected, 'store_id') else selected.id)
            background_tasks.add_task(_run_and_reply, message_text, sender, selected.id)
            return _twiml(f"Got it! Checking on {selected.name}... report coming in ~30–60s.")
        else:
            return _twiml(_store_menu(stores))

    # 4. Determine active store
    if len(stores) == 1:
        store_id = stores[0].id
    else:
        if session and session.store_id:
            store_id = session.store_id
        else:
            # Multiple stores, no active session — prompt selection
            set_user_session(sender, None)
            return _twiml(_store_menu(stores))

    # 5. Run pipeline in background
    background_tasks.add_task(_run_and_reply, message_text, sender, store_id)
    return _twiml("Got it! Analysing competitors... report coming in ~30–60s.")


def _run_and_reply(message_text: str, sender: str, store_id: int) -> None:
    try:
        command = classify_intent(message_text)
        if command == "switch":
            command = "scout"
        logger.info("Classified %r → %s (store_id=%d)", message_text[:60], command, store_id)
        report = _strip_freshness(pipeline.run(command, store_id=store_id, user_message=message_text))
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


# ---------------------------------------------------------------------------
# Admin endpoints (store + member management)
# ---------------------------------------------------------------------------

@app.post("/admin/stores")
async def create_store(
    name: Annotated[str, Form()],
    location: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    instagram_handle: Annotated[str, Form()] = "",
):
    """Create a new store and seed its competitors from the default seed list."""
    from app.config import COMPETITORS
    from app.db import Competitor, _seed_competitors
    with SessionLocal() as db:
        store = Store(
            name=name,
            location=location or None,
            category=category or None,
            instagram_handle=instagram_handle or None,
        )
        db.add(store)
        db.commit()
        db.refresh(store)
        _seed_competitors(store.id, db, COMPETITORS)
    return JSONResponse({"store_id": store.id, "name": store.name})


@app.post("/admin/stores/{store_id}/members")
async def add_member(
    store_id: int,
    whatsapp: Annotated[str, Form()],
    role: Annotated[str, Form()] = "owner",
):
    """Register a WhatsApp number as a member of a store."""
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")
        existing = db.query(StoreMember).filter(
            StoreMember.store_id == store_id,
            StoreMember.whatsapp == whatsapp,
        ).first()
        if existing:
            return JSONResponse({"status": "already_exists", "store_id": store_id, "whatsapp": whatsapp})
        db.add(StoreMember(store_id=store_id, whatsapp=whatsapp, role=role))
        db.commit()
    return JSONResponse({"status": "added", "store_id": store_id, "whatsapp": whatsapp, "role": role})


@app.get("/admin/stores")
async def list_stores():
    """List all registered stores."""
    with SessionLocal() as db:
        stores = db.query(Store).all()
        return JSONResponse([
            {"id": s.id, "name": s.name, "location": s.location, "category": s.category}
            for s in stores
        ])


# ---------------------------------------------------------------------------
# Pipeline HTTP endpoints (for testing without WhatsApp)
# ---------------------------------------------------------------------------

@app.post("/scout")
async def scout_endpoint():
    return await run_command("scout")


@app.post("/run/{command}")
async def run_command(command: str, store_id: int = 1):
    command = command.lower().strip()
    if command not in VALID_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command '{command}'. Valid: {sorted(VALID_COMMANDS)}",
        )
    try:
        report = pipeline.run(command, store_id=store_id)
        with SessionLocal() as db:
            latest_run = (
                db.query(Run)
                .filter(Run.store_id == store_id, Run.status.in_(["ok", "partial"]))
                .order_by(Run.finished_at.desc())
                .first()
            )
            run_id = latest_run.id if latest_run else None
            finding_count = latest_run.finding_count if latest_run else 0
        return JSONResponse({
            "report": report,
            "command": command,
            "store_id": store_id,
            "run_id": run_id,
            "findings_count": finding_count,
        })
    except Exception as exc:
        logger.error("/run/%s error: %s", command, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/report/latest")
async def latest_report(command: str = "scout", store_id: int = 1):
    with SessionLocal() as db:
        report = (
            db.query(Report)
            .filter(Report.store_id == store_id, Report.command == command)
            .order_by(Report.created_at.desc())
            .first()
        )
    if not report:
        raise HTTPException(status_code=404, detail=f"No report found for command '{command}'")
    return JSONResponse({
        "command": report.command,
        "store_id": store_id,
        "report_text": report.report_text,
        "created_at": str(report.created_at),
        "run_id": report.run_id,
    })
