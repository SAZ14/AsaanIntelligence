"""Natural-language understanding for inbound WhatsApp queue messages.

The LLM turns free text ("table for 4, name's Ayesha") into a structured
ParsedMessage. A deterministic regex parser is the fallback whenever no
client is supplied or the API call fails -- so the agent (and the test
suite) never hard-depend on the network.

Per the original design: *the LLM understands, code decides.* Nothing here
joins or leaves the queue; it only extracts intent + slots.

Ported from the maitre-d-agent branch's Claude-based parser
(anthropic.Anthropic, claude-haiku-4-5) onto this server's shared ZAI
client (app.core.llm.get_client/get_fast_model) -- every other agent's LLM
calls go through that same client, and pulling in a second provider/SDK
just for this one agent's NLU would mean a second API key and a real-money
dependency the rest of the platform doesn't have.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

INTENTS = ("greeting", "book", "cancel", "help", "unknown")

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a couple": 2, "couple": 2,
}


@dataclass
class ParsedMessage:
    intent: str = "unknown"
    party_size: int | None = None
    name: str = ""
    special_requests: str = ""
    confidence: str = "low"          # "high" (LLM) | "low" (fallback)
    raw: str = ""


def parse_message(
    text: str, *, now: datetime | None = None, client=None,
) -> ParsedMessage:
    """Parse one inbound message. Uses the LLM when a client is supplied,
    else the deterministic fallback. `client` is a ZAI/OpenAI-compatible
    client (app.core.llm.get_client()), not required -- pass None to force
    the fallback (used by tests that don't want network calls). `now` is
    accepted for API symmetry with callers that pass a venue clock, but
    nothing here currently depends on the time of day."""
    if client is not None:
        parsed = _parse_with_llm(text, client)
        if parsed is not None:
            return parsed
    return _parse_fallback(text)


# ── LLM NLU ──

def _parse_with_llm(text: str, client) -> ParsedMessage | None:
    from app.core.llm import get_fast_model, nothink_kwargs

    prompt = f"""You are the NLU for a restaurant's WhatsApp walk-in queue line.
Extract structured queue-joining info from the guest's message. Reply with
ONLY a JSON object, no prose.

Fields:
- intent: one of {list(INTENTS)}
    greeting = hi/hello only; book = wants to join the queue/get a table;
    cancel = leave the queue/cancel; help = asks hours/menu/info;
    unknown = none of these
- party_size: integer or null
- name: the guest's name if stated, else ""
- special_requests: e.g. "window table", "birthday", "high chair", else ""

Message: "{text}"

JSON:"""
    try:
        model = get_fast_model()
        resp = client.chat.completions.create(
            model=model,
            max_tokens=150,
            # get_client()'s underlying OpenAI client has max_retries=1 baked
            # in at construction -- a per-call timeout is a ceiling PER
            # ATTEMPT, not overall, so a genuine stall here can cost up to
            # 2x this value before falling back to the deterministic parser.
            # Confirmed live: a 10.0 timeout produced two ~20s replies in a
            # row when the first attempt stalled. Kept tight (matching the
            # router's fast-classification budget in gateway/internal.py)
            # since the fallback parser exists precisely so a slow/failed
            # LLM call never blocks a reply for long.
            timeout=6.0,
            messages=[{"role": "user", "content": prompt}],
            **nothink_kwargs(model),
        )
        body = resp.choices[0].message.content.strip()
        body = _strip_code_fence(body)
        data = json.loads(body)
    except Exception:
        return None

    intent = data.get("intent", "unknown")
    if intent not in INTENTS:
        intent = "unknown"

    party = data.get("party_size")
    party = int(party) if isinstance(party, (int, float)) and party else None

    return ParsedMessage(
        intent=intent,
        party_size=party,
        name=(data.get("name") or "").strip(),
        special_requests=(data.get("special_requests") or "").strip(),
        confidence="high",
        raw=text,
    )


def _strip_code_fence(body: str) -> str:
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body)
    return body.strip()


# ── Deterministic fallback ──

def _parse_fallback(text: str) -> ParsedMessage:
    lower = text.lower().strip()

    intent = _fallback_intent(lower)
    party = _extract_party_size(lower)
    name = _extract_name(text)
    requests = _extract_requests(lower)

    # A bare party size with no explicit verb is almost certainly a booking.
    if intent == "unknown" and party:
        intent = "book"

    return ParsedMessage(
        intent=intent,
        party_size=party,
        name=name,
        special_requests=requests,
        confidence="low",
        raw=text,
    )


def _fallback_intent(lower: str) -> str:
    if re.search(r"\bcancel\b", lower):
        return "cancel"
    if re.search(r"\b(book|reserve|reservation|table|seat|party of|queue|line|walk[- ]?in)\b", lower):
        return "book"
    if re.search(r"\b(hi|hello|hey|salam|assalam|aoa|good (morning|evening))\b", lower):
        return "greeting"
    if re.search(r"\b(help|hours|open|menu|location|where)\b", lower):
        return "help"
    return "unknown"


def _extract_party_size(lower: str) -> int | None:
    # "table for 4", "for 4", "party of 4", "4 people/pax/guests/of us"
    patterns = [
        r"(?:table|booking|reservation)\s+for\s+(\d{1,2})",
        r"party of\s+(\d{1,2})",
        r"\bfor\s+(\d{1,2})\b",
        r"(\d{1,2})\s*(?:people|persons|pax|guests|heads|of us|adults)",
        r"(?:we are|we're|there are|its|it's)\s+(\d{1,2})\b",
    ]
    for pat in patterns:
        m = re.search(pat, lower)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 30:
                return n
    for word, n in NUMBER_WORDS.items():
        if re.search(rf"(?:for|of|party of|table for)\s+{re.escape(word)}\b", lower):
            return n
    return None


def _extract_name(text: str) -> str:
    # "it's Ayesha", "name is Bilal", "this is Sana", "I'm Omar"
    m = re.search(
        r"\b(?:it'?s|i'?m|i am|this is|name'?s|name is|under)\s+([A-Z][a-z]+)\b",
        text,
    )
    return m.group(1) if m else ""


def _extract_requests(lower: str) -> str:
    found = []
    for kw in ("window", "outdoor", "outside", "booth", "high chair", "birthday",
               "anniversary", "wheelchair", "quiet", "terrace", "corner"):
        if kw in lower:
            found.append(kw)
    return ", ".join(found)
