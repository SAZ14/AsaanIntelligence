"""FastAPI endpoint for Twilio's inbound WhatsApp webhook.

The owner replies to an alert with one of:
  - POST              → publish the drafted reply as-is
  - EDIT <new text>   → publish a revised reply
  - IGNORE            → skip this review
  - <free text>       → treated as a custom reply to publish

Twilio delivers an `application/x-www-form-urlencoded` POST with at least
`Body` and `From`. We parse the command, route POST/EDIT/free-text to the
`post_reply` stub (later wired per-platform), and reply with TwiML so the
owner gets an acknowledgement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import Response

logger = logging.getLogger("whatsapp.webhook")

# Action constants
POST = "POST"
EDIT = "EDIT"
IGNORE = "IGNORE"
FREETEXT = "FREETEXT"
EMPTY = "EMPTY"


@dataclass
class Command:
    action: str        # one of POST / EDIT / IGNORE / FREETEXT / EMPTY
    text: str = ""     # revised/custom reply text where applicable
    ack: str = ""      # human-readable acknowledgement


def parse_command(body: str) -> Command:
    """Parse the owner's reply into a structured command.

    Keyword match is case-insensitive and tolerant of surrounding whitespace.
    """
    raw = (body or "").strip()
    if not raw:
        return Command(action=EMPTY, ack="Empty message — nothing to do.")

    head, _, rest = raw.partition(" ")
    keyword = head.strip().upper()
    rest = rest.strip()

    if keyword == POST:
        return Command(action=POST, ack="Posting the suggested reply.")
    if keyword == IGNORE:
        return Command(action=IGNORE, ack="Ignored — no reply will be posted.")
    if keyword == EDIT:
        if rest:
            return Command(action=EDIT, text=rest, ack="Posting your edited reply.")
        return Command(action=EMPTY, ack="EDIT received but no text followed it.")

    # No recognised keyword → treat the whole message as a custom reply.
    return Command(action=FREETEXT, text=raw, ack="Posting your custom reply.")


def post_reply(platform: str, review_id: str, text: str) -> None:
    """Stub for publishing a reply back to the review platform.

    Wired per-platform later (Google/Foodpanda/Instagram). For now it logs
    so the routing is observable end to end.
    """
    logger.info("post_reply → platform=%s review_id=%s text=%r", platform, review_id, text)


def handle_inbound(body: str, from_number: str = "", review_id: str = "", platform: str = "") -> Command:
    """Core routing logic, independent of HTTP — directly unit-testable.

    Returns the parsed Command after performing any side effects (post_reply).
    """
    cmd = parse_command(body)

    if cmd.action == POST:
        post_reply(platform=platform, review_id=review_id, text="")  # publish drafted reply
    elif cmd.action in (EDIT, FREETEXT):
        post_reply(platform=platform, review_id=review_id, text=cmd.text)
    elif cmd.action == IGNORE:
        logger.info("ignore → review_id=%s from=%s", review_id, from_number)
    else:  # EMPTY
        logger.info("empty/no-op → from=%s", from_number)

    return cmd


def _twiml(message: str) -> Response:
    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>{message}</Message></Response>"
    return Response(content=xml, media_type="application/xml")


app = FastAPI(title="Reputation WhatsApp webhook")


@app.post("/whatsapp/inbound")
async def whatsapp_inbound(request: Request) -> Response:
    """Twilio inbound webhook.

    Twilio posts `application/x-www-form-urlencoded`; we parse the raw body
    ourselves so the service has no python-multipart dependency. Form fields
    follow Twilio's casing (Body/From).
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    fields = parse_qs(raw)
    body = fields.get("Body", [""])[0]
    from_number = fields.get("From", [""])[0]
    cmd = handle_inbound(body, from_number=from_number)
    return _twiml(cmd.ack)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
