from __future__ import annotations
import json
import logging
import re
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import app.agents.scout.config as _cfg
from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)

ENRICH_SYSTEM = (
    "You are a competitive-intelligence analyst for Sugar Rush, a dessert & ice-cream shop "
    "in Islamabad. For each competitor finding, write a one-sentence summary of what it is "
    "and why it matters to Sugar Rush, and rate its relevance to Sugar Rush from 1 (irrelevant) "
    "to 10 (urgent/high-impact). Return strict JSON: a list of {\"id\", \"summary\", \"relevance_score\"}. "
    "No prose outside JSON."
)

REPORT_SYSTEM = (
    "You advise the owner of Sugar Rush (dessert & ice cream, Islamabad). "
    "Be specific: name competitors and their concrete moves, cite engagement/ratings when present. "
    "Give concrete, doable actions (limited-time bundles, specific reel ideas, counter-offers). "
    "Never be generic. Keep it tight and skimmable for WhatsApp. "
    "Use short numbered points, minimal emoji."
)

COMMAND_INSTRUCTIONS = {
    "scout": (
        "Write a full competitive intelligence report with these sections:\n"
        "1. SUMMARY (2-3 sentences on the competitive landscape right now)\n"
        "2. TOP COMPETITOR MOVES (numbered, each with competitor name + what they did + why it matters)\n"
        "3. NEW PRODUCTS & OFFERS (specific items, prices if available)\n"
        "4. CAMPAIGNS & CONTENT TRENDS\n"
        "5. OPPORTUNITIES FOR SUGAR RUSH (concrete gaps)\n"
        "6. SUGGESTED ACTIONS (3-5 specific, doable moves this week)\n"
        "7. URGENCY: Low / Medium / High — with one sentence justifying it."
    ),
    "alerts": (
        "List ONLY the highest-impact recent competitor moves — new product launches, "
        "unusually high-engagement posts, new offers/discounts, or major campaigns. "
        "Skip anything routine. For each: competitor name, what happened, why urgent."
    ),
    "competitors": (
        "For each competitor mentioned in the findings, write 1-2 lines describing "
        "what they are currently doing online (content strategy, recent posts, promotions). "
        "Be specific about what you see in the data."
    ),
    "campaigns": (
        "Identify all current promotions, seasonal campaigns (Eid/summer/winter/Valentine), "
        "and content trends across competitors. Note which platforms they use and what engagement "
        "they're getting. Suggest 2-3 campaign ideas Sugar Rush could run in response."
    ),
    "opportunities": (
        "Identify 3-5 concrete gaps or underserved moments Sugar Rush can exploit based on "
        "what competitors are NOT doing or doing poorly. For each opportunity: what the gap is, "
        "why now, and a specific action Sugar Rush can take this week."
    ),
    "pricing": (
        "Report any pricing or menu signals found in the data — specific prices, deals, "
        "value offers, or bundle pricing. If price data is unavailable, say so explicitly "
        "and describe menu/product signals instead. Do not invent prices."
    ),
    "content": (
        "Analyze what content types are getting the best engagement across competitors "
        "(cakes vs ice cream vs brownies, reels vs static posts, seasonal vs evergreen). "
        "Give Sugar Rush 3-5 specific content ideas based on what is actually working."
    ),
    "help": (
        "List the available commands for the Sugar Rush competitive scout agent:\n"
        "- scout: Full competitive intelligence report (live fetch)\n"
        "- alerts: Highest-impact competitor moves right now\n"
        "- competitors: What each competitor is doing online\n"
        "- campaigns: Current promotions and seasonal campaigns\n"
        "- opportunities: Gaps Sugar Rush can exploit\n"
        "- pricing: Competitor pricing and menu signals\n"
        "- content: Top-performing content types + Sugar Rush content ideas\n"
        "- help: Show this list\n\nSend any command to get started."
    ),
}


def _get_client():
    if not _cfg.ZAI_API_KEY:
        raise RuntimeError("ZAI_API_KEY not set")
    from openai import OpenAI
    return OpenAI(api_key=_cfg.ZAI_API_KEY, base_url="https://open.bigmodel.cn/api/paas/v4/")


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


def _chat(system: str, user: str) -> str:
    client = _get_client()
    resp = client.chat.completions.create(
        model=_cfg.ZAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
    )
    return resp.choices[0].message.content


MAX_ENRICH = 60


def enrich_findings(findings: list[FindingSchema]) -> list[FindingSchema]:
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

    for i in range(0, len(to_enrich), BATCH):
        batch = to_enrich[i: i + BATCH]
        batch = results[i: i + BATCH]
        items_json = json.dumps([
            {"id": idx, "competitor": f.competitor_name, "type": f.update_type, "text": f.content_text[:400]}
            for idx, f in enumerate(batch)
        ], ensure_ascii=False)

        prompt = f"Findings:\n{items_json}"
        try:
            raw = _chat(ENRICH_SYSTEM, prompt)
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


_INTENT_SYSTEM = (
    "You route WhatsApp messages to the right competitive intelligence report for Sugar Rush, "
    "a dessert & ice cream shop in Islamabad.\n"
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

_VALID_INTENTS = {"scout", "alerts", "competitors", "campaigns", "opportunities", "pricing", "content", "help", "switch"}


def classify_intent(message: str) -> str:
    """Map a free-form user message to the closest pipeline command."""
    if not _cfg.ZAI_API_KEY:
        return "scout"
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=_cfg.ZAI_MODEL,
            messages=[
                {"role": "system", "content": _INTENT_SYSTEM},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=10,
        )
        intent = resp.choices[0].message.content.strip().lower()
        if intent in _VALID_INTENTS:
            return intent
    except Exception as exc:
        logger.warning("Intent classification failed: %s", exc)
    return "scout"


def build_report(command: str, findings: list[FindingSchema], freshness_note: str,
                 user_message: Optional[str] = None) -> str:
    cmd = command.lower().strip()

    if cmd == "help":
        return COMMAND_INSTRUCTIONS["help"]

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

    instruction = COMMAND_INSTRUCTIONS.get(cmd, COMMAND_INSTRUCTIONS["scout"])

    top = sorted(findings, key=lambda f: -(f.relevance_score or 0))[:25]

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
        report = _chat(REPORT_SYSTEM, prompt)
        return f"{freshness_note}\n\n{report}"
    except Exception as exc:
        logger.error("ZAI report generation failed: %s", exc)
        return (
            f"{freshness_note}\n\n"
            f"Report generation failed: {exc}. "
            f"Raw findings count: {len(findings)}."
        )
