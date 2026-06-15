"""FastAPI router for the WhatsApp Cloud API webhook.

GET  /webhook  — Meta verification handshake (echoes hub.challenge).
POST /webhook  — inbound messages: routes button taps and free text.

Mount it on an app:

    from fastapi import FastAPI
    from whatsapp.webhook import router
    app = FastAPI()
    app.include_router(router)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse

from .config import WhatsAppConfig
from .notifier import BTN_EDIT, BTN_IGNORE, BTN_POST_REPLY

log = logging.getLogger("whatsapp.webhook")

router = APIRouter()


# ── Inbound parsing (pure / testable) ──

@dataclass
class ParsedMessage:
    from_number: str = ""
    message_id: str = ""
    kind: str = "other"          # "button" | "text" | "other"
    button_id: str = ""
    button_title: str = ""
    text: str = ""


def parse_webhook_events(body: dict[str, Any]) -> list[ParsedMessage]:
    """Flatten a Cloud API webhook body into a list of ParsedMessage.

    Tolerates status-only callbacks (delivery receipts) and partial shapes by
    skipping anything without a recognisable message.
    """
    parsed: list[ParsedMessage] = []
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                pm = ParsedMessage(
                    from_number=msg.get("from", ""),
                    message_id=msg.get("id", ""),
                )
                mtype = msg.get("type")
                if mtype == "interactive":
                    interactive = msg.get("interactive", {}) or {}
                    if interactive.get("type") == "button_reply":
                        reply = interactive.get("button_reply", {}) or {}
                        pm.kind = "button"
                        pm.button_id = reply.get("id", "")
                        pm.button_title = reply.get("title", "")
                elif mtype == "button":
                    # Legacy template-button quick reply.
                    btn = msg.get("button", {}) or {}
                    pm.kind = "button"
                    pm.button_id = btn.get("payload", "")
                    pm.button_title = btn.get("text", "")
                elif mtype == "text":
                    pm.kind = "text"
                    pm.text = (msg.get("text", {}) or {}).get("body", "")
                parsed.append(pm)
    return parsed


# ── Action handlers (stubs to be wired to Google later) ──

def post_reply(msg: ParsedMessage) -> None:
    """Owner tapped 'Post reply' — later this posts the draft to Google."""
    log.info("ACTION post_reply from=%s message_id=%s (stub: would post draft to Google)",
             msg.from_number, msg.message_id)


def handle_ignore(msg: ParsedMessage) -> None:
    log.info("ACTION ignore from=%s message_id=%s", msg.from_number, msg.message_id)


def handle_edit(msg: ParsedMessage) -> None:
    """Owner tapped 'Edit' — later this opens an edit flow; for now just log."""
    log.info("ACTION edit from=%s message_id=%s (stub: awaiting edited reply text)",
             msg.from_number, msg.message_id)


def handle_qa(msg: ParsedMessage) -> None:
    """Free-text from the owner — optional Q&A handler stub."""
    log.info("ACTION qa from=%s text=%r (stub: would answer owner question)",
             msg.from_number, msg.text)


def dispatch(msg: ParsedMessage) -> str:
    """Route a single parsed message to its handler. Returns the action name."""
    if msg.kind == "button":
        if msg.button_id == BTN_POST_REPLY:
            post_reply(msg)
            return "post_reply"
        if msg.button_id == BTN_IGNORE:
            handle_ignore(msg)
            return "ignore"
        if msg.button_id == BTN_EDIT:
            handle_edit(msg)
            return "edit"
        log.info("Unknown button id=%r", msg.button_id)
        return "unknown_button"
    if msg.kind == "text" and msg.text:
        handle_qa(msg)
        return "qa"
    return "ignored"


# ── Routes ──

@router.get("/webhook")
def verify(
    mode: str = Query("", alias="hub.mode"),
    verify_token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Meta verification handshake: echo hub.challenge when the token matches."""
    config = WhatsAppConfig.from_env()
    if mode == "subscribe" and verify_token and verify_token == config.verify_token:
        return PlainTextResponse(content=challenge, status_code=200)
    return PlainTextResponse(content="verification failed", status_code=403)


@router.post("/webhook")
async def receive(request: Request) -> dict[str, Any]:
    """Parse inbound messages and route button taps / free text."""
    body = await request.json()
    actions = [dispatch(msg) for msg in parse_webhook_events(body)]
    return {"status": "ok", "actions": actions}
