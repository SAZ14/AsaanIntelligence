"""Understand the owner's free-text questions.

Claude maps a message to an intent + reporting period; a deterministic keyword
parser is the fallback so the agent (and tests) never hard-depend on the network.
Claude only parses — all numbers and advice are computed in code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

INTENTS = (
    "summary",        # overall revenue/how-did-we-do
    "best_sellers",   # what's selling
    "pricing",        # which prices can I raise
    "dead_windows",   # when are we slow
    "campaigns",      # ideas to boost revenue / fill slow times
    "strategy",       # full revenue-growth playbook (all levers)
    "upsell",         # raise average ticket: attach / bundles / add-ons
    "menu",           # menu optimisation: heroes, dogs, gaps
    "loyalty",        # frequency: loyalty programme / win-back
    "subscribe",      # set up scheduled digests
    "help",
    "unknown",
)


@dataclass
class ParsedQuery:
    intent: str = "unknown"
    period: str = "week"          # day | week | month
    cadence: str = ""             # for subscribe: daily | weekly | monthly
    confidence: str = "low"
    raw: str = ""


def parse_query(text: str, client: anthropic.Anthropic | None = None) -> ParsedQuery:
    if client is not None:
        parsed = _parse_with_claude(text, client)
        if parsed is not None:
            return parsed
    return _parse_fallback(text)


def _parse_with_claude(text: str, client: anthropic.Anthropic) -> ParsedQuery | None:
    prompt = f"""You are the NLU for a café owner's revenue-advisor WhatsApp line.
Map the message to JSON only (no prose).

Fields:
- intent: one of {list(INTENTS)}
    summary = overall performance/revenue; best_sellers = what's selling;
    pricing = which prices to raise; dead_windows = slow/quiet times;
    campaigns = ideas to fill quiet windows specifically;
    strategy = broad "how do I grow / maximise revenue" advice (all levers);
    upsell = raise average ticket / spend per order (attach, bundles, combos, add-ons);
    menu = menu optimisation (high-margin items, low-margin dogs, what to add);
    loyalty = repeat business / frequency / loyalty programme / win-back;
    subscribe = wants regular/scheduled updates; help = what can you do; unknown
- period: "day", "week" or "month" (default "week")
- cadence: if intent is subscribe, "daily"/"weekly"/"monthly", else ""

Message: "{text}"
JSON:"""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        body = resp.content[0].text.strip()
        if body.startswith("```"):
            body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
            body = re.sub(r"\n?```$", "", body).strip()
        data = json.loads(body)
    except Exception:
        return None

    intent = data.get("intent", "unknown")
    if intent not in INTENTS:
        intent = "unknown"
    return ParsedQuery(
        intent=intent,
        period=_norm_period(data.get("period", "week")),
        cadence=(data.get("cadence") or "").strip().lower(),
        confidence="high", raw=text,
    )


def _parse_fallback(text: str) -> ParsedQuery:
    lower = text.lower().strip()
    period = _norm_period(lower)
    cadence = ""

    if re.search(r"\b(subscribe|every day|every week|digest|each (day|week|month)|send me .*(update|digest|report))\b", lower):
        intent = "subscribe"
        if "dai" in lower or "every day" in lower:
            cadence = "daily"
        elif "month" in lower:
            cadence = "monthly"
        else:
            cadence = "weekly"
    elif re.search(r"\b(price|prices|pricing|raise (the )?price|charge more|increase price|markup)\b", lower):
        intent = "pricing"
    elif re.search(r"\b(upsell|up.?sell|average (ticket|transaction|order|spend)|spend per|basket|add.?on|attach|combo|bundle|pastry|bigger (order|ticket)|increase spend)", lower):
        intent = "upsell"
    elif re.search(r"\b(loyal|repeat|frequency|come back|retention|punch card|win.?back|regular)", lower):
        intent = "loyalty"
    elif re.search(r"\b(menu|high.?margin|low.?margin|margin|product mix|what should i (add|sell|cut)|dishes to add|cut item|dog)", lower):
        intent = "menu"
    elif re.search(r"\b(strateg|maximi|grow|growth|increase revenue|more revenue|more money|make more|boost revenue|playbook|how (do|can) i (grow|increase|make|boost))", lower):
        intent = "strategy"
    elif re.search(r"\b(campaign|promo|promotion|fill|event|underutil)\b", lower):
        intent = "campaigns"
    elif re.search(r"\b(dead|slow|quiet|empty|lull|off.?peak|low demand)\b", lower):
        intent = "dead_windows"
    elif re.search(r"\b(best.?sell|top.?sell|selling|most popular|top item|top product|what sold)", lower):
        intent = "best_sellers"
    elif re.search(r"\b(revenue|sales|how (did|are) we|summary|overview|performance|takings|total)\b", lower):
        intent = "summary"
    elif re.search(r"\b(help|what can you|commands|options)\b", lower):
        intent = "help"
    else:
        intent = "unknown"

    return ParsedQuery(intent=intent, period=period, cadence=cadence,
                       confidence="low", raw=text)


def _norm_period(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(today|day|daily|24 ?h)\b", t):
        return "day"
    if re.search(r"\b(month|monthly|30 ?d)\b", t):
        return "month"
    return "week"

