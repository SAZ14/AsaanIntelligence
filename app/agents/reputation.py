from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models.canonical import MenuItem, Order, Review, Staff

logger = logging.getLogger(__name__)


# ── Config ──

DEFAULT_VENUE_NAME = "Sugar Rush"
DEFAULT_BRAND_VOICE = (
    "Warm, appreciative, specific. Thank by name, reference their order "
    "when possible, acknowledge issues honestly, invite them back."
)

ISSUE_CLASSES = [
    "service_speed", "staff_attitude", "food_quality",
    "price", "ambiance", "praise", "other",
]

CLASSIFIER_BATCH_SIZE = 15
HISTORICAL_CUTOFF_DAYS = 7

DAY_KEYWORDS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "weekend": (5, 6), "weekday": (0, 1, 2, 3, 4),
}

TIME_PATTERNS = [
    (re.compile(r"(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm)", re.I), True),
    (re.compile(r"~?\s*(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm|ish)", re.I), True),
    (re.compile(r"around\s+(\d{1,2})\s*(pm|am)?", re.I), True),
    (re.compile(r"\b(morning|afternoon|evening|night)\b", re.I), False),
]

DAYPART_HOURS = {
    "morning": (7, 12), "afternoon": (12, 17),
    "evening": (17, 22), "night": (19, 23),
}


# ── Data classes ──

@dataclass
class BrandVoice:
    name: str = DEFAULT_VENUE_NAME
    tone: str = DEFAULT_BRAND_VOICE
    never_say: list[str] = field(default_factory=list)


@dataclass
class VisitContext:
    estimated_date: str = ""
    estimated_hour_range: str = ""
    order_count_in_window: int = 0
    staff_on_duty: list[str] = field(default_factory=list)
    matched_staff_id: str = ""
    matched_staff_name: str = ""
    confidence: str = "none"
    match_reasons: list[str] = field(default_factory=list)


@dataclass
class ReviewAnalysis:
    review_id: str
    source: str
    rating: int
    posted_at: str
    reviewer_name: str
    text: str
    correlation: VisitContext = field(default_factory=VisitContext)
    issue_class: str = "other"
    sentiment: str = "neutral"
    draft_reply: str = ""


@dataclass
class PatternFinding:
    issue: str
    day_of_week: str = ""
    hour_range: str = ""
    review_count: int = 0
    review_ids: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ReputationReport:
    venue_name: str = ""
    total_reviews: int = 0
    avg_rating: float = 0.0
    reviews: list[ReviewAnalysis] = field(default_factory=list)
    patterns: list[PatternFinding] = field(default_factory=list)
    happy_reviewers: list[dict] = field(default_factory=list)


# ── Correlation engine (deterministic) ──

def _extract_day_of_week(text: str) -> int | None:
    lower = text.lower()
    for kw, val in DAY_KEYWORDS.items():
        if kw in lower:
            if isinstance(val, tuple):
                return val[0]
            return val
    return None


def _extract_hour_range(text: str) -> tuple[int, int] | None:
    lower = text.lower()
    for pat, has_time in TIME_PATTERNS:
        m = pat.search(lower)
        if m:
            if has_time and m.group(1).isdigit():
                h = int(m.group(1))
                ampm = m.group(3).lower() if m.group(3) else ""
                if ampm in ("pm", "ish") and h < 12:
                    h += 12
                elif ampm == "am" and h == 12:
                    h = 0
                return (max(h - 1, 0), min(h + 1, 23))
            elif not has_time:
                word = m.group(1).lower()
                if word in DAYPART_HOURS:
                    return DAYPART_HOURS[word]
    return None


def _extract_staff_name(text: str, staff: dict[str, Staff]) -> tuple[str, str] | None:
    lower = text.lower()
    for sid, s in staff.items():
        name_lower = s.name.lower()
        if re.search(r'\b' + re.escape(name_lower) + r'\b', lower):
            return sid, s.name
    return None


def _extract_item_mentions(text: str, menu: dict[str, MenuItem]) -> list[str]:
    lower = text.lower()
    found = []
    for sku, mi in menu.items():
        if mi.name.lower() in lower:
            found.append(mi.name)
    return found


def correlate_review(
    review: Review,
    orders: list[Order],
    staff: dict[str, Staff],
    menu: dict[str, MenuItem],
) -> VisitContext:
    ctx = VisitContext()
    reasons: list[str] = []

    mentioned_dow = _extract_day_of_week(review.text)
    mentioned_hours = _extract_hour_range(review.text)
    staff_match = _extract_staff_name(review.text, staff)
    item_mentions = _extract_item_mentions(review.text, menu)

    posted = review.posted_at
    candidate_dates: list[datetime] = []

    if mentioned_dow is not None:
        for delta in range(0, 8):
            d = (posted - timedelta(days=delta)).date()
            if d.weekday() == mentioned_dow:
                candidate_dates.append(datetime.combine(d, datetime.min.time()))
                reasons.append(f"text mentions {d.strftime('%A')}")
                break
    else:
        yesterday = (posted - timedelta(days=1)).date()
        today = posted.date()
        candidate_dates.append(datetime.combine(yesterday, datetime.min.time()))
        candidate_dates.append(datetime.combine(today, datetime.min.time()))

    if not candidate_dates and not staff_match:
        ctx.confidence = "none"
        return ctx

    hour_lo, hour_hi = (0, 23)
    if mentioned_hours:
        hour_lo, hour_hi = mentioned_hours
        reasons.append(f"text mentions ~{hour_lo}:00–{hour_hi}:00")

    best_date = None
    best_count = 0
    best_staff_list: list[str] = []

    for cand in candidate_dates:
        d = cand.date()
        window_orders = [
            o for o in orders
            if o.datetime.date() == d and hour_lo <= o.datetime.hour <= hour_hi
        ]
        if len(window_orders) > best_count or best_date is None:
            best_date = d
            best_count = len(window_orders)
            staff_ids_in_window = {o.staff_id for o in window_orders}
            best_staff_list = sorted(staff_ids_in_window)

    if best_date:
        ctx.estimated_date = str(best_date)
        ctx.estimated_hour_range = f"{hour_lo:02d}:00–{hour_hi:02d}:00"
        ctx.order_count_in_window = best_count
        ctx.staff_on_duty = best_staff_list

    if staff_match:
        ctx.matched_staff_id = staff_match[0]
        ctx.matched_staff_name = staff_match[1]
        reasons.append(f"text names staff '{staff_match[1]}'")

    if item_mentions:
        reasons.append(f"mentions items: {', '.join(item_mentions)}")

    ctx.match_reasons = reasons

    if staff_match or (mentioned_dow is not None and mentioned_hours):
        ctx.confidence = "high"
    elif mentioned_dow is not None or mentioned_hours:
        ctx.confidence = "medium"
    elif item_mentions:
        ctx.confidence = "low"
    else:
        ctx.confidence = "none"

    return ctx


# ── LLM classification (batched) ──

def classify_reviews_batch(
    reviews: list[ReviewAnalysis],
    client,
    cutoff_days: int = HISTORICAL_CUTOFF_DAYS,
) -> list[ReviewAnalysis]:
    from app.core.llm import get_model

    now = datetime.utcnow()
    historical: list[ReviewAnalysis] = []
    recent: list[ReviewAnalysis] = []

    for ra in reviews:
        try:
            posted = datetime.strptime(ra.posted_at[:10], "%Y-%m-%d")
            age = (now - posted).days
        except Exception:
            age = 0
        if age > cutoff_days:
            historical.append(ra)
        else:
            recent.append(ra)

    # Rule-based for older reviews (no LLM cost)
    for ra in historical:
        if ra.rating >= 4:
            ra.issue_class = "praise"
            ra.sentiment = "positive"
        elif ra.rating <= 2:
            ra.issue_class = "service_speed"
            ra.sentiment = "negative"
        else:
            ra.issue_class = "other"
            ra.sentiment = "neutral"

    # Batch LLM for recent reviews
    for i in range(0, len(recent), CLASSIFIER_BATCH_SIZE):
        batch = recent[i : i + CLASSIFIER_BATCH_SIZE]
        lines = [
            f"{j}. [Rating {ra.rating}/5] \"{ra.text[:200]}\""
            for j, ra in enumerate(batch, 1)
        ]
        prompt = (
            f"Classify each review. Reply with one line per review: N. issue_class,sentiment\n"
            f"Issue classes: {', '.join(ISSUE_CLASSES)}\n"
            "Sentiments: positive, negative, neutral, mixed\n\n"
            + "\n".join(lines)
        )
        try:
            resp = client.chat.completions.create(
                model=get_model(),
                max_tokens=CLASSIFIER_BATCH_SIZE * 12,
                messages=[{"role": "user", "content": prompt}],
            )
            output = resp.choices[0].message.content.strip()
            for line in output.split("\n"):
                m = re.match(r"(\d+)\.\s*(\w+)\s*,\s*(\w+)", line.strip())
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(batch):
                        issue = m.group(2).strip().lower()
                        sent = m.group(3).strip().lower()
                        if issue in ISSUE_CLASSES:
                            batch[idx].issue_class = issue
                        if sent in ("positive", "negative", "neutral", "mixed"):
                            batch[idx].sentiment = sent
        except Exception as exc:
            logger.warning("Batch classify failed: %s", exc)

    return reviews


# ── Pattern detection (deterministic) ──

def detect_patterns(
    reviews: list[ReviewAnalysis],
) -> list[PatternFinding]:
    clusters: dict[tuple[str, str, str], list[ReviewAnalysis]] = defaultdict(list)

    for ra in reviews:
        if ra.correlation.confidence == "none":
            continue
        if ra.issue_class in ("praise", "other"):
            continue
        dow = ""
        if ra.correlation.estimated_date:
            try:
                d = datetime.strptime(ra.correlation.estimated_date, "%Y-%m-%d")
                dow = d.strftime("%A")
            except ValueError:
                pass
        key = (ra.issue_class, dow, ra.correlation.estimated_hour_range)
        clusters[key].append(ra)

    patterns: list[PatternFinding] = []
    for (issue, dow, hours), ras in clusters.items():
        if len(ras) >= 2:
            patterns.append(PatternFinding(
                issue=issue,
                day_of_week=dow,
                hour_range=hours,
                review_count=len(ras),
                review_ids=[r.review_id for r in ras],
                description=(
                    f"{len(ras)} reviews flag {issue.replace('_', ' ')} on "
                    f"{dow + ' ' if dow else ''}{hours if hours else 'unknown time'}"
                ),
            ))

    patterns.sort(key=lambda p: p.review_count, reverse=True)
    return patterns


# ── Response drafting (LLM) ──

def draft_replies(
    reviews: list[ReviewAnalysis],
    client,
    venue_name: str | None = None,
    brand_voice: str | BrandVoice | None = None,
) -> list[ReviewAnalysis]:
    from app.core.llm import get_model

    if isinstance(brand_voice, BrandVoice):
        effective_venue = venue_name or brand_voice.name or DEFAULT_VENUE_NAME
        never_say_str = (
            f"Never use these words/phrases: {', '.join(brand_voice.never_say)}. "
            if brand_voice.never_say
            else ""
        )
        system_msg = (
            f"You write public review responses on behalf of {effective_venue}. "
            f"Tone: {brand_voice.tone}. "
            f"{never_say_str}"
            "Reply directly — no quotation marks, no intro like 'Here is a reply:'."
        )
    else:
        effective_venue = venue_name or DEFAULT_VENUE_NAME
        bv_tone = brand_voice or DEFAULT_BRAND_VOICE
        system_msg = (
            f"You write public review responses on behalf of {effective_venue}. "
            f"Brand voice: {bv_tone}. "
            "Reply directly — no quotation marks."
        )

    for ra in reviews:
        if ra.rating >= 5 and ra.issue_class == "praise":
            tone = "thankful, invite them to try something new"
        elif ra.rating <= 2:
            tone = "apologetic, acknowledge the specific issue, explain what you're doing about it"
        elif ra.rating <= 3:
            tone = "appreciative of feedback, address concern"
        else:
            tone = "warm thank you"

        context_lines = []
        if ra.correlation.confidence != "none":
            if ra.correlation.estimated_date:
                context_lines.append(f"Visit was likely {ra.correlation.estimated_date}")
            if ra.correlation.order_count_in_window > 0:
                context_lines.append(f"{ra.correlation.order_count_in_window} orders in that window (busy period)")
            if ra.correlation.matched_staff_name:
                context_lines.append(f"Staff involved: {ra.correlation.matched_staff_name}")
        context_str = "; ".join(context_lines) if context_lines else "No visit details available"

        prompt = (
            f"Write a short reply (2-3 sentences) from {effective_venue} to this review.\n\n"
            f"Tone: {tone}\n"
            f"Review by {ra.reviewer_name} ({ra.rating}/5 on {ra.source}): \"{ra.text}\"\n"
            f"Visit context: {context_str}\n"
            f"Issue: {ra.issue_class}\n\n"
            "Be specific to their experience, not generic. Do not use emojis."
        )

        try:
            resp = client.chat.completions.create(
                model=get_model(),
                max_tokens=150,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
            )
            ra.draft_reply = resp.choices[0].message.content.strip()
        except Exception as e:
            ra.draft_reply = f"[draft generation failed: {e}]"

    return reviews


# ── Main agent ──

def run_reputation_agent(
    reviews: list[Review],
    orders: list[Order],
    staff: dict[str, Staff],
    menu: dict[str, MenuItem],
    venue_name: str = DEFAULT_VENUE_NAME,
    brand_voice: str | BrandVoice | None = None,
    client=None,
) -> ReputationReport:
    if client is None:
        from app.core.llm import get_client
        client = get_client()

    if brand_voice is None:
        brand_voice = DEFAULT_BRAND_VOICE

    analyses: list[ReviewAnalysis] = []
    for r in reviews:
        ctx = correlate_review(r, orders, staff, menu)
        analyses.append(ReviewAnalysis(
            review_id=r.review_id,
            source=r.source,
            rating=r.rating,
            posted_at=str(r.posted_at),
            reviewer_name=r.reviewer_name,
            text=r.text,
            correlation=ctx,
        ))

    classify_reviews_batch(analyses, client)
    patterns = detect_patterns(analyses)
    draft_replies(analyses, client, venue_name=venue_name, brand_voice=brand_voice)

    happy = [
        {"reviewer_name": ra.reviewer_name, "review_id": ra.review_id,
         "rating": ra.rating, "source": ra.source}
        for ra in analyses
        if ra.rating >= 5 and ra.sentiment == "positive"
    ]

    avg_rating = sum(r.rating for r in reviews) / len(reviews) if reviews else 0

    return ReputationReport(
        venue_name=venue_name,
        total_reviews=len(reviews),
        avg_rating=avg_rating,
        reviews=analyses,
        patterns=patterns,
        happy_reviewers=happy,
    )


# ── WhatsApp owner reply handler ──

def process_reputation_owner_reply(from_phone: str, body: str) -> str:
    """Handle a reputation-related WhatsApp message from an owner/staff member.

    Returns the reply string; the gateway handles sending it back via TwiML.
    """
    from app.core.db import (
        get_stores_for_number, get_user_session, set_user_session,
        SessionLocal, VenueConfig,
    )
    from app.review_sources import db as review_db

    stores = get_stores_for_number(from_phone)
    if not stores:
        return "You are not registered as a staff member for any store."

    if len(stores) == 1:
        active_store = stores[0]
    else:
        session = get_user_session(from_phone)
        matched = next(
            (s for s in stores if s.id == (session.store_id if session else None)),
            None,
        )
        active_store = matched or stores[0]

    set_user_session(from_phone, active_store.id)
    store_id = active_store.id

    with SessionLocal() as db:
        vc = db.query(VenueConfig).filter(VenueConfig.store_id == store_id).first()
    store_name = (vc.venue_name if vc else None) or active_store.name

    text = body.strip()
    text_lower = text.lower()

    if text_lower.startswith("post"):
        finding = review_db.get_pending_finding(store_id)
        if not finding:
            return f"[{store_name}] No pending review drafts awaiting confirmation."
        summary = finding["ai_summary"]
        summary["status"] = "posted"
        review_db.update_finding_summary(finding["id"], summary)
        draft = summary.get("draft_reply", "")
        return f"[{store_name}] Published draft response:\n\n{draft}"

    if text_lower.startswith("edit"):
        new_draft = text[4:].strip()
        if not new_draft:
            return f"[{store_name}] Reply with *EDIT <your new message>* to revise the draft."
        finding = review_db.get_pending_finding(store_id)
        if not finding:
            return f"[{store_name}] No pending review draft found to edit."
        summary = finding["ai_summary"]
        summary["draft_reply"] = new_draft
        review_db.update_finding_summary(finding["id"], summary)
        return (
            f"[{store_name}] Draft updated to:\n\n{new_draft}\n\n"
            "Reply *POST* to publish or *IGNORE* to skip."
        )

    if text_lower.startswith("ignore"):
        finding = review_db.get_pending_finding(store_id)
        if not finding:
            return f"[{store_name}] No pending review draft to ignore."
        summary = finding["ai_summary"]
        summary["status"] = "ignored"
        review_db.update_finding_summary(finding["id"], summary)
        return f"[{store_name}] Skipped — no reply will be posted."

    if any(kw in text_lower for kw in ("check", "scrape", "crawl", "sync")):
        return _check_reviews(store_id, store_name)

    return _chat_about_reviews(store_id, store_name, text)


def _check_reviews(store_id: int, store_name: str) -> str:
    from app.review_sources.pipeline import run_pipeline
    from app.review_sources import db as review_db
    from app.review_sources.normalizer import to_review_model
    from app.core.llm import get_client

    try:
        raw_reviews = run_pipeline()
    except Exception as exc:
        logger.error("review pipeline error: %s", exc)
        return f"[{store_name}] Review check failed: {exc}"

    if not raw_reviews:
        return f"[{store_name}] No new reviews found across all platforms."

    run_id = review_db.save_run(store_id, "whatsapp_check")
    client = get_client()

    analyses: list[tuple[dict, ReviewAnalysis]] = []
    for r in raw_reviews:
        try:
            rev_model = to_review_model(r)
            ctx = correlate_review(rev_model, [], {}, {})
            ra = ReviewAnalysis(
                review_id=rev_model.review_id,
                source=rev_model.source,
                rating=rev_model.rating,
                posted_at=str(rev_model.posted_at),
                reviewer_name=rev_model.reviewer_name,
                text=rev_model.text,
                correlation=ctx,
            )
            analyses.append((r, ra))
        except Exception as exc:
            logger.warning("Skipping review: %s", exc)

    classify_reviews_batch([ra for _, ra in analyses], client)
    draft_replies([ra for _, ra in analyses], client, venue_name=store_name)

    new_count = 0
    for raw, ra in analyses:
        ai_summary = {
            "status": "pending",
            "sentiment": ra.sentiment,
            "issue_class": ra.issue_class,
            "draft_reply": ra.draft_reply,
            "correlation": {
                "estimated_date": ra.correlation.estimated_date,
                "matched_staff_name": ra.correlation.matched_staff_name,
                "confidence": ra.correlation.confidence,
            },
        }
        if review_db.save_review_finding(store_id, run_id, store_name, raw, ai_summary):
            new_count += 1

    review_db.update_run(run_id, "ok", ["pipeline"], [], new_count)

    if new_count == 0:
        return f"[{store_name}] No new reviews found. All up to date."

    pending = review_db.get_pending_finding(store_id)
    if pending:
        summary = pending["ai_summary"]
        rating = pending.get("rating") or "N/A"
        excerpt = (pending.get("content_text") or "")[:150]
        draft = summary.get("draft_reply", "")
        return (
            f"[{store_name}] Processed {new_count} new reviews.\n\n"
            f"Latest pending:\n"
            f"⭐ {rating}/5: \"{excerpt}\"\n\n"
            f"Suggested reply:\n{draft}\n\n"
            "Reply *POST* to publish · *EDIT <text>* to revise · *IGNORE* to skip"
        )

    return f"[{store_name}] Processed {new_count} new reviews."


def _chat_about_reviews(store_id: int, store_name: str, text: str) -> str:
    from app.review_sources import db as review_db
    from app.core.llm import get_client, get_model

    pending = review_db.get_pending_finding(store_id)
    recent_reviews = review_db.get_recent_reviews(store_id, limit=10)

    pending_ctx = ""
    if pending:
        s = pending.get("ai_summary") or {}
        corr = s.get("correlation") or {}
        pending_ctx = (
            "\nPENDING REVIEW:\n"
            f"- Rating: {pending.get('rating')}/5\n"
            f"- Text: \"{(pending.get('content_text') or '')[:200]}\"\n"
            f"- Suggested reply: \"{s.get('draft_reply', '')}\"\n"
            f"- Served by: {corr.get('matched_staff_name') or 'unknown'}\n"
        )

    recent_ctx = "RECENT REVIEWS:\n"
    if recent_reviews:
        for i, r in enumerate(recent_reviews, 1):
            recent_ctx += (
                f"{i}. [{r.get('source')}] {r.get('rating') or 'N/A'}/5: "
                f"\"{(r.get('text') or '')[:100]}\"\n"
            )
    else:
        recent_ctx += "(None — type CHECK to scrape new reviews)\n"

    system = (
        f"You assist the owner of '{store_name}' with review management. "
        "Be concise — this is WhatsApp.\n"
        "Commands: POST (publish draft), EDIT <text> (revise draft), "
        "IGNORE (skip), CHECK (scrape new reviews).\n\n"
        f"{pending_ctx}\n{recent_ctx}"
    )

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=get_model(),
            max_tokens=500,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("Reputation chat error: %s", exc)
        return (
            f"[{store_name}] Commands:\n"
            "*POST* — publish draft reply\n"
            "*EDIT <text>* — revise the draft\n"
            "*IGNORE* — skip this review\n"
            "*CHECK* — scrape new reviews"
        )
