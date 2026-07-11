from __future__ import annotations
import json
import logging
import re
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import app.agents.scout.config as _cfg
from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)


class ReportGenerationFailed(Exception):
    """Raised when build_report()'s final LLM call fails (timeout, API
    error) -- distinct from a routine "no findings" case. Confirmed live:
    build_report() used to swallow this and return a
    "Report generation failed: ..." STRING as if it were a normal report,
    which run() then persisted as a real ScoutReport row with no way to
    tell it apart from a genuine one later -- one such placeholder got
    resurfaced days later as "the most recent report on file" when a
    different failure needed a fallback. Raising instead lets callers
    (run(), answer_from_cache()) decide explicitly: never persist this,
    fall back to the last genuinely good report where one exists."""


def _enrich_system(store_name: str, store_category: str) -> str:
    return (
        f"You are a competitive-intelligence analyst for {store_name}, a {store_category} restaurant. "
        f"For each competitor finding, write a one-sentence summary of what it is "
        f"and why it matters to {store_name}, and rate its relevance from 1 (irrelevant) "
        "to 10 (urgent/high-impact). Return strict JSON: a list of {\"id\", \"summary\", \"relevance_score\"}. "
        "No prose outside JSON."
    )


def _report_system(store_name: str, store_category: str) -> str:
    from app.core.persona import staff_persona
    return (
        staff_persona(f"You advise the owner of {store_name} ({store_category} restaurant) on competitive intelligence.")
        + "\nCite exact evidence from the data whenever it's present -- prices, ratings, "
        "engagement numbers, dates, quotes. Give concrete, actionable steps: specific bundle "
        "ideas, reel concepts, counter-offers. Never be generic, and never pad a thin finding "
        "with filler sentences that add no new information; conversely, don't compress a "
        "well-evidenced competitor into one throwaway line just to save space. Match length "
        "to how much real signal exists for each item -- substance over brevity, a report "
        "that's slightly longer and actually useful beats one that's short and empty. "
        "Numbered lists or bullet points with - are fine for multiple items."
    )


def _command_instructions(store_name: str) -> dict[str, str]:
    return {
        "scout": (
            "Write a full competitive intelligence report with these sections:\n"
            "1. SUMMARY (2-3 sentences on the competitive landscape right now)\n"
            "2. TOP COMPETITOR MOVES (numbered; each gets a real paragraph, not a one-liner: "
            "competitor name, what they did with specific evidence (price/rating/engagement), and why it matters)\n"
            "3. NEW PRODUCTS & OFFERS (specific items, prices if available)\n"
            "4. CAMPAIGNS & CONTENT TRENDS\n"
            f"5. OPPORTUNITIES FOR {store_name.upper()} (concrete gaps)\n"
            "6. SUGGESTED ACTIONS (3-5 specific, doable moves this week)\n"
            "7. URGENCY: Low / Medium / High, with one sentence justifying it."
        ),
        "alerts": (
            "List ONLY the highest-impact recent competitor moves: new product launches, "
            "unusually high-engagement posts, new offers/discounts, or major campaigns. "
            "Skip anything routine. For each: competitor name, what happened (with the actual "
            "numbers/prices/dates from the data), and why it's urgent enough to act on now."
        ),
        "competitors": (
            "For each competitor that has real signal in the findings, write a short paragraph "
            "covering what they're doing right now: cite the actual evidence (specific menu items, "
            "prices, promotions, ratings, or engagement numbers), not a vague description. "
            f"Add a brief line on why it matters for {store_name} or what to consider doing about it. "
            "Competitors with rich findings deserve more space; a competitor with only one thin "
            "finding should get one honest sentence, not padding."
        ),
        "campaigns": (
            "Identify all current promotions, seasonal campaigns (Eid/summer/winter/Valentine), "
            "and content trends across competitors. Note which platforms they use and what engagement "
            f"they're getting. Suggest 2-3 campaign ideas {store_name} could run in response."
        ),
        "opportunities": (
            f"Identify 3-5 concrete gaps or underserved moments {store_name} can exploit based on "
            "what competitors are NOT doing or doing poorly. For each opportunity: what the gap is, "
            f"why now, and a specific action {store_name} can take this week."
        ),
        "pricing": (
            "Report any pricing or menu signals found in the data: specific prices, deals, "
            "value offers, or bundle pricing. If price data is unavailable, say so explicitly "
            "and describe menu/product signals instead. Do not invent prices."
        ),
        "content": (
            "Analyze what content types are getting the best engagement across competitors "
            "(reels vs static posts, seasonal vs evergreen, food vs lifestyle). "
            f"Give {store_name} 3-5 specific content ideas based on what is actually working."
        ),
        "help": (
            f"List the available commands for the {store_name} competitive scout agent:\n"
            "- scout: Full competitive intelligence report (live fetch)\n"
            "- alerts: Highest-impact competitor moves right now\n"
            "- competitors: What each competitor is doing online\n"
            "- campaigns: Current promotions and seasonal campaigns\n"
            f"- opportunities: Gaps {store_name} can exploit\n"
            "- pricing: Competitor pricing and menu signals\n"
            f"- content: Top-performing content types + {store_name} content ideas\n"
            "- help: Show this list\n\nSend any command to get started."
        ),
    }


def _intent_system(store_name: str, store_category: str) -> str:
    return (
        f"You route WhatsApp messages to the right competitive intelligence report for {store_name}, "
        f"a {store_category} restaurant.\n"
        "Map the user's message to exactly one of these commands:\n"
        "  scout       - general update, full report, 'what's happening', unclear intent\n"
        "  alerts      - urgent moves, threats, 'anything important', latest news\n"
        "  competitors - asking about specific competitors or what they're doing\n"
        "  campaigns   - promotions, deals, offers, discounts, campaigns\n"
        "  opportunities - gaps, what should we do, how to compete, strategy\n"
        "  pricing     - prices, menu costs, discounts, value\n"
        "  content     - social media, what to post, reels, content ideas\n"
        "  help        - asking what the bot can do or how to use it\n"
        "  switch      - user wants to change restaurant / switch store / talk about a different business\n"
        "Reply with ONLY the command name. No punctuation, no explanation."
    )


def _get_client():
    from app.core.llm import get_client
    return get_client()


def _get_model() -> str:
    from app.core.llm import get_model
    return get_model()


def _extract_json(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return []


def _chat(system: str, user: str, history: list[dict] | None = None) -> str:
    from app.core.llm import nothink_kwargs

    client = _get_client()
    model = _get_model()
    # Confirmed live: with GLM's thinking mode left on (the previous
    # behavior here), report generation took 37.7s on a small test prompt
    # and, at real production scale (60 findings across 12 competitors),
    # occasionally exceeded this call's own 90s timeout -- which, combined
    # with the shared client's max_retries=1, meant an outright report
    # failure ~7 minutes into an otherwise-successful run (confirmed live:
    # "Report generation failed: Request timed out." after 144 findings
    # were already scraped and ready). nothink_kwargs cut the same test
    # prompt to 11.4s with no measurable quality loss on a realistic
    # multi-competitor report (still cites exact numbers, still gives
    # concrete per-competitor recommendations, still keeps a thin finding
    # to one honest sentence) -- reliability wins over a marginal
    # reasoning benefit for what is fundamentally a "write from the given
    # evidence" task, not a multi-step reasoning problem. timeout=90
    # overrides the shared client's 30s interactive default as headroom
    # for the largest reports; max_tokens gives the denser per-competitor
    # paragraphs REPORT_DEPTH_POLICY asks for room to actually use.
    messages = [{"role": "system", "content": system}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user})
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.4,
        timeout=90,
        max_tokens=2000,
        **nothink_kwargs(model),
    )
    return resp.choices[0].message.content


MAX_ENRICH = 20


def enrich_findings(
    findings: list[FindingSchema],
    store_name: str = "the restaurant",
    store_category: str = "food",
) -> list[FindingSchema]:
    """Batch enrich findings with ai_summary and relevance_score."""
    if not findings:
        return findings
    if not _cfg.ZAI_API_KEY:
        logger.warning("ZAI disabled — skipping enrichment")
        return findings

    to_enrich = [f for f in findings if not f.ai_summary][:MAX_ENRICH]
    if not to_enrich:
        return findings

    BATCH = 15
    results = list(findings)
    enrich_system = _enrich_system(store_name, store_category)

    for i in range(0, len(to_enrich), BATCH):
        batch = to_enrich[i: i + BATCH]
        batch = results[i: i + BATCH]
        items_json = json.dumps([
            {"id": idx, "competitor": f.competitor_name, "type": f.update_type, "text": f.content_text[:400]}
            for idx, f in enumerate(batch)
        ], ensure_ascii=False)

        prompt = f"Findings:\n{items_json}"
        try:
            raw = _chat(enrich_system, prompt)
            parsed = _extract_json(raw)
            if isinstance(parsed, list):
                lookup = {item.get("id"): item for item in parsed if isinstance(item, dict)}
                for local_idx, finding in enumerate(batch):
                    entry = lookup.get(local_idx, {})
                    finding.ai_summary = entry.get("summary") or finding.ai_summary
                    score = entry.get("relevance_score")
                    if score is not None:
                        try:
                            finding.relevance_score = max(1, min(10, int(score)))
                        except (TypeError, ValueError):
                            finding.relevance_score = 5
                    elif finding.relevance_score is None:
                        finding.relevance_score = 5
        except Exception as exc:
            logger.error("ZAI enrichment failed for batch %d: %s", i // BATCH, exc)
            for finding in batch:
                if finding.relevance_score is None:
                    finding.relevance_score = 5

    return results



_VALID_INTENTS = {"scout", "alerts", "competitors", "campaigns", "opportunities", "pricing", "content", "help", "switch"}


def classify_intent(
    message: str,
    store_name: str = "the restaurant",
    store_category: str = "food",
) -> str:
    """Map a free-form user message to the closest pipeline command."""
    if not _cfg.ZAI_API_KEY:
        return "scout"
    try:
        from app.core.llm import get_fast_model, nothink_kwargs
        client = _get_client()
        fast_model = get_fast_model()
        # 10-token classification — fast non-reasoning model, not the
        # thinking model used for report generation in _chat().
        resp = client.chat.completions.create(
            timeout=8.0,
            model=fast_model,
            messages=[
                {"role": "system", "content": _intent_system(store_name, store_category)},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=10,
            **nothink_kwargs(fast_model),
        )
        intent = resp.choices[0].message.content.strip().lower()
        if intent in _VALID_INTENTS:
            return intent
    except Exception as exc:
        logger.warning("Intent classification failed: %s", exc)
    return "scout"


def classify_target_competitor(message: str, competitor_names: list[str]) -> str | None:
    """Which single competitor (if any) this free-form question is about,
    or None for a general/multi-competitor question.

    A different problem from classify_intent above (which command bucket)
    -- this decides WHICH competitor, so pipeline.run() can fetch that
    competitor's full finding history instead of the normal report
    selection's REPORT_FINDINGS_PER_COMPETITOR=4 cap, which exists to
    keep a many-competitor report balanced but is exactly the wrong
    limit when a staff member asked about one specific competitor and
    wants everything known about them, not four data points."""
    if not competitor_names or not _cfg.ZAI_API_KEY:
        return None
    try:
        from app.core.llm import get_fast_model, nothink_kwargs
        client = _get_client()
        fast_model = get_fast_model()
        numbered = "\n".join(f"{i+1}. {name}" for i, name in enumerate(competitor_names))
        system = (
            "A restaurant owner is asking a question about their competitors. "
            "Here is the exact list of competitors being tracked:\n"
            f"{numbered}\n\n"
            "If the question is asking about ONE SPECIFIC competitor from this list "
            "(by name, a close/partial match, or an obvious nickname), reply with "
            "EXACTLY that competitor's name as written above, nothing else.\n"
            "If the question is general (asks about competitors overall, multiple "
            "competitors, or doesn't clearly name one from the list), reply with "
            "exactly: NONE\n"
            "No punctuation, no explanation, just the name or NONE."
        )
        resp = client.chat.completions.create(
            timeout=8.0,
            model=fast_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=30,
            **nothink_kwargs(fast_model),
        )
        answer = resp.choices[0].message.content.strip()
        if answer in competitor_names:
            return answer
    except Exception as exc:
        logger.warning("classify_target_competitor failed: %s", exc)
    return None


# Applied to every command instruction except "help". Confirmed live: with
# the old flat "write 1-2 lines per competitor" instruction, a run that
# processed 1500+ findings produced a report with a single throwaway
# sentence per competitor -- all that scraping and enrichment cost, thrown
# away at the last step. This makes length follow evidence instead of a
# fixed line cap.
REPORT_DEPTH_POLICY = (
    " Depth policy: match how much you write to how much real evidence exists for that "
    "item -- a competitor with several concrete findings (prices, ratings, engagement, specific "
    "products) earns a real paragraph that uses them, not a compressed one-liner; a competitor "
    "with only one thin finding earns one honest sentence, not padding. Never write a sentence "
    "that contains no actual information from the data."
)

# Per-competitor quota for the findings passed into the report-generation
# prompt. A flat global top-N (the old approach) lets 2-3 highly-scored
# competitors crowd out every other competitor's evidence entirely --
# exactly why the report above had nothing to say about most of them.
# Guaranteeing each competitor its own slice means the model always has
# something concrete to write about whichever competitors are mentioned.
REPORT_FINDINGS_PER_COMPETITOR = 4
REPORT_MAX_TOTAL_FINDINGS = 60


def _select_report_findings(findings: list[FindingSchema]) -> list[FindingSchema]:
    by_competitor: dict[str, list[FindingSchema]] = {}
    for f in findings:
        by_competitor.setdefault(f.competitor_name, []).append(f)

    def _signal(f: FindingSchema) -> tuple:
        extremity = abs((f.rating if f.rating is not None else 3.0) - 3.0)
        engagement = sum(v for v in (f.engagement or {}).values() if isinstance(v, (int, float)))
        return (-(f.relevance_score or 0), -extremity, -engagement)

    selected: list[FindingSchema] = []
    for items in by_competitor.values():
        items_sorted = sorted(items, key=_signal)
        selected.extend(items_sorted[:REPORT_FINDINGS_PER_COMPETITOR])

    if len(selected) > REPORT_MAX_TOTAL_FINDINGS:
        # Trim the globally weakest signal rather than dropping whole
        # competitors -- preserves breadth over depth when still over budget.
        selected.sort(key=_signal)
        selected = selected[:REPORT_MAX_TOTAL_FINDINGS]
    return selected


def build_report(
    command: str,
    findings: list[FindingSchema],
    freshness_note: str,
    user_message: Optional[str] = None,
    store_name: str = "the restaurant",
    store_category: str = "food",
    history: list[dict] | None = None,
) -> str:
    cmd = command.lower().strip()
    instructions = _command_instructions(store_name)

    if cmd == "help":
        return instructions["help"]

    if not _cfg.ZAI_API_KEY:
        return (
            f"{freshness_note}\n\n"
            "AI analysis unavailable (ZAI_API_KEY not set). "
            f"Found {len(findings)} raw findings but cannot generate a report."
        )

    if not findings:
        return (
            f"{freshness_note}\n\n"
            "No competitor signals found in this run. Sources may have failed or returned empty results. "
            "Try again shortly or check your API keys."
        )

    instruction = instructions.get(cmd, instructions["scout"])
    if cmd != "help":
        instruction += REPORT_DEPTH_POLICY

    top = _select_report_findings(findings)

    findings_text = "\n\n".join(
        f"[{f.competitor_name} | {f.source_platform} | {f.update_type}]\n"
        f"{f.content_text[:400]}"
        + (f"\nAI: {f.ai_summary}" if f.ai_summary else "")
        + (f"\nRelevance: {f.relevance_score}/10" if f.relevance_score else "")
        + (f"\nEngagement: {f.engagement}" if f.engagement else "")
        + (f"\nRating: {f.rating}" if f.rating else "")
        for f in top
    )

    user_context = f"\nThe user asked: \"{user_message}\"\nTailor your response to directly answer their question.\n" if user_message else ""

    prompt = (
        f"Instruction: {instruction}\n"
        f"{user_context}\n"
        f"Competitor findings:\n{findings_text}"
    )

    try:
        report = _chat(_report_system(store_name, store_category), prompt, history=history)
        return f"{freshness_note}\n\n{report}"
    except Exception as exc:
        logger.error("ZAI report generation failed: %s", exc)
        raise ReportGenerationFailed(str(exc)) from exc
