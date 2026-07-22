"""Natural-language understanding for inbound WhatsApp booking messages.

The LLM turns free text ("table for 4 this Friday around 8ish, it's Ayesha")
into a structured ParsedMessage. A deterministic regex parser is the
fallback whenever no client is supplied or the API call fails -- so the
agent (and the test suite) never hard-depend on the network.

Per the original design: *the LLM understands, code decides.* Nothing here
books, cancels or holds a table; it only extracts intent + slots.

Ported from the maitre-d-agent branch's Claude-based parser
(anthropic.Anthropic, claude-haiku-4-5) onto this server's shared ZAI
client (app.core.llm.get_client/get_fast_model) -- every other agent's LLM
calls go through that same client, and pulling in a second provider/SDK
just for this one agent's NLU would mean a second API key and a real-money
dependency the rest of the platform doesn't have. The deterministic
fallback parser is unchanged from the original branch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

INTENTS = (
    "greeting", "book", "cancel", "modify",
    "confirm", "decline", "help", "unknown",
)

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a couple": 2, "couple": 2,
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

DAYPART_DEFAULT_HOUR = {
    "breakfast": 9, "brunch": 11, "lunch": 13,
    "tea": 17, "evening": 19, "dinner": 20, "tonight": 20, "night": 21,
}


@dataclass
class ParsedMessage:
    intent: str = "unknown"
    party_size: int | None = None
    when: datetime | None = None
    date_only: bool = False          # a date was given but no specific time
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
    the fallback (used by tests that don't want network calls)."""
    now = now or datetime.now()
    if client is not None:
        parsed = _parse_with_llm(text, now, client)
        if parsed is not None:
            return parsed
    return _parse_fallback(text, now)


# ── LLM NLU ──

def _parse_with_llm(text: str, now: datetime, client) -> ParsedMessage | None:
    from app.core.llm import get_fast_model, nothink_kwargs

    prompt = f"""You are the NLU for a restaurant reservations WhatsApp line.
Extract structured booking info from the guest's message. Reply with ONLY a
JSON object, no prose.

Now: {now.strftime('%A %Y-%m-%d %H:%M')} (timezone Asia/Karachi)

Fields:
- intent: one of {list(INTENTS)}
    greeting = hi/hello only; book = wants a table; cancel = cancel a booking;
    modify = change an existing booking; confirm = yes/agree/accept;
    decline = no/reject; help = asks hours/menu/info; unknown = none of these
- party_size: integer or null
- datetime: ISO 8601 "YYYY-MM-DDTHH:MM" resolved against Now, or null if no
    time/date is given. If only a date is given, use "YYYY-MM-DD".
- name: the guest's name if stated, else ""
- special_requests: e.g. "window table", "birthday", "high chair", else ""

Message: "{text}"

JSON:"""
    try:
        model = get_fast_model()
        resp = client.chat.completions.create(
            model=model,
            max_tokens=200,
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

    when, date_only = _coerce_datetime(data.get("datetime"), now)
    party = data.get("party_size")
    party = int(party) if isinstance(party, (int, float)) and party else None

    return ParsedMessage(
        intent=intent,
        party_size=party,
        when=when,
        date_only=date_only,
        name=(data.get("name") or "").strip(),
        special_requests=(data.get("special_requests") or "").strip(),
        confidence="high",
        raw=text,
    )


def _coerce_datetime(value, now: datetime) -> tuple[datetime | None, bool]:
    if not value or not isinstance(value, str):
        return None, False
    try:
        if "T" in value:
            return datetime.fromisoformat(value), False
        # date only
        d = datetime.fromisoformat(value)
        return d, True
    except ValueError:
        return None, False


def _strip_code_fence(body: str) -> str:
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body)
    return body.strip()


# ── Deterministic fallback ──

def _parse_fallback(text: str, now: datetime) -> ParsedMessage:
    lower = text.lower().strip()

    intent = _fallback_intent(lower)
    party = _extract_party_size(lower)
    when, date_only = _extract_datetime(lower, now)
    name = _extract_name(text)
    requests = _extract_requests(lower)

    # A bare time/party with no explicit verb is almost certainly a booking.
    if intent == "unknown" and (party or when):
        intent = "book"

    return ParsedMessage(
        intent=intent,
        party_size=party,
        when=when,
        date_only=date_only,
        name=name,
        special_requests=requests,
        confidence="low",
        raw=text,
    )


def _fallback_intent(lower: str) -> str:
    if re.search(r"\bcancel\b", lower):
        return "cancel"
    if re.search(r"\b(reschedul|change|move|instead|push (it )?to)\b", lower):
        return "modify"
    if re.search(r"\b(book|reserve|reservation|table|seat|party of)\b", lower):
        return "book"
    if re.search(r"\b(yes|yep|yeah|confirm|confirmed|sure|ok|okay|paid|deal|done)\b", lower):
        return "confirm"
    if re.search(r"\b(no|nope|can'?t|cannot|decline|never\s?mind|nvm)\b", lower):
        return "decline"
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


def _extract_datetime(lower: str, now: datetime) -> tuple[datetime | None, bool]:
    target_date = _extract_date(lower, now)
    hour, minute = _extract_time(lower)

    if hour is None:
        if target_date is not None:
            return target_date.replace(hour=0, minute=0, second=0, microsecond=0), True
        return None, False

    base = target_date or now
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If no date was stated and that time has already passed today, roll to tomorrow.
    if target_date is None and when <= now:
        when += timedelta(days=1)
    return when, False


def _extract_date(lower: str, now: datetime) -> datetime | None:
    if re.search(r"\b(today|tonight)\b", lower):
        return now
    if re.search(r"\bday after tomorrow\b", lower):
        return now + timedelta(days=2)
    if re.search(r"\btomorrow\b", lower):
        return now + timedelta(days=1)

    # weekday names, optionally "next"
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lower):
            wants_next = bool(re.search(rf"\bnext\s+{name}\b", lower))
            return _next_weekday(now, wd, force_next_week=wants_next)

    # explicit "15 june" / "june 15" / "15/6" / "15-06"
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", lower)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        return _safe_date(now, month, day)
    months = ("january february march april may june july august "
              "september october november december").split()
    m = re.search(r"\b(\d{1,2})\s+([a-z]+)\b", lower)
    if m and m.group(2) in months:
        return _safe_date(now, months.index(m.group(2)) + 1, int(m.group(1)))
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})\b", lower)
    if m and m.group(1) in months:
        return _safe_date(now, months.index(m.group(1)) + 1, int(m.group(2)))
    return None


def _extract_time(lower: str) -> tuple[int | None, int | None]:
    # 8pm, 8:30pm, 8 pm, 20:00, "at 8", "8ish"
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lower)
    if m:
        h = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return h, minute
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", lower)  # 24h
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})\s*ish\b", lower)
    if m:
        h = int(m.group(1))
        if h <= 11:  # "8ish" means evening
            h += 12
        return h, 0
    m = re.search(r"\bat\s+(\d{1,2})\b", lower)
    if m:
        h = int(m.group(1))
        if h <= 11:
            h += 12
        return h, 0
    for word, h in DAYPART_DEFAULT_HOUR.items():
        if re.search(rf"\b{word}\b", lower):
            return h, 0
    return None, None


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


def _next_weekday(now: datetime, weekday: int, force_next_week: bool) -> datetime:
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7 if force_next_week else 0
    elif force_next_week:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _safe_date(now: datetime, month: int, day: int) -> datetime | None:
    year = now.year
    try:
        cand = datetime(year, month, day)
    except ValueError:
        return None
    # If the date already passed this year, assume next year.
    if cand.date() < now.date():
        try:
            cand = datetime(year + 1, month, day)
        except ValueError:
            return None
    return cand
