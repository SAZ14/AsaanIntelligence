from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models.canonical import MenuItem, Order, Review, Staff

logger = logging.getLogger(__name__)


# ── Config ──

DEFAULT_VENUE_NAME = "the venue"
DEFAULT_BRAND_VOICE = (
    "Warm, appreciative, specific. Thank by name, reference their order "
    "when possible, acknowledge issues honestly, invite them back."
)

ISSUE_CLASSES = [
    "service_speed", "staff_attitude", "food_quality",
    "price", "ambiance", "praise", "other",
]

CLASSIFIER_BATCH_SIZE = 30
HISTORICAL_CUTOFF_DAYS = 3
MAX_REVIEWS_PER_CHECK = 50
MAX_DRAFT_REPLIES = 5
MAX_POSITIVE_DRAFT_REPLIES = 5
# Matches the cron cadence in scripts/run_server.py (3x/day, ~8h apart) --
# by the time a staff member checks, a scheduled run should always have
# happened within this window, so their check hits cache instantly instead
# of waiting on a live Apify scrape.
REPUTATION_CACHE_HOURS = 8

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
                try:
                    ampm = (m.group(3) or "").lower()
                except IndexError:
                    ampm = (m.group(2) or "").lower()
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
    from app.core.llm import get_model, nothink_kwargs

    now = datetime.utcnow()
    historical: list[ReviewAnalysis] = []
    recent: list[ReviewAnalysis] = []

    for ra in reviews:
        try:
            posted = datetime.strptime(ra.posted_at[:10], "%Y-%m-%d")
            age = (now - posted).days
        except Exception:
            age = 0
        # The rule-based path below has no signal to work with for unrated
        # platforms (Instagram comments have no stars) -- it can only look
        # at rating, so an unrated item there gets a placeholder ("neutral")
        # rather than real analysis of what it actually says. Route unrated
        # items through the LLM regardless of age so they get genuine
        # text-based classification instead.
        if age > cutoff_days and ra.rating:
            historical.append(ra)
        else:
            recent.append(ra)

    # Rule-based for older RATED reviews only (no LLM cost)
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

    # Batch LLM for recent reviews (and any unrated item, regardless of age)
    for i in range(0, len(recent), CLASSIFIER_BATCH_SIZE):
        batch = recent[i : i + CLASSIFIER_BATCH_SIZE]
        lines = [
            # "[Rating 0/5]" reads as the worst possible score, not "no
            # rating provided" -- misleading for platforms like Instagram
            # that have no star ratings at all. Say so plainly instead.
            f"{j}. [{f'Rating {ra.rating}/5' if ra.rating else 'No star rating (platform has none)'}] "
            f"\"{ra.text[:200]}\""
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
                timeout=60.0,
                model=get_model(),
                max_tokens=CLASSIFIER_BATCH_SIZE * 12,
                messages=[{"role": "user", "content": prompt}],
                **nothink_kwargs(get_model()),
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
    from app.core.llm import get_model, nothink_kwargs

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
            "Reply directly, no quotation marks, no intro like 'Here is a reply:'."
        )
    else:
        effective_venue = venue_name or DEFAULT_VENUE_NAME
        bv_tone = brand_voice or DEFAULT_BRAND_VOICE
        system_msg = (
            f"You write public review responses on behalf of {effective_venue}. "
            f"Brand voice: {bv_tone}. "
            "Reply directly, no quotation marks."
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
            "Be specific to their experience, not generic. Do not use emojis. "
            "No em-dashes -- use a comma or colon instead."
        )

        try:
            resp = client.chat.completions.create(
                timeout=20.0,
                model=get_model(),
                max_tokens=150,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                **nothink_kwargs(get_model()),
            )
            ra.draft_reply = resp.choices[0].message.content.strip()
        except Exception as e:
            ra.draft_reply = f"[draft generation failed: {e}]"

    return reviews


def select_reviews_needing_reply(analyses: list[ReviewAnalysis]) -> list[ReviewAnalysis]:
    """Which reviews get a drafted reply and enter the scrollable pending
    queue (post/edit/ignore). Both rated negative reviews (apologetic/
    appreciative tone, per draft_replies' own logic) and rated positive ones
    (thankful/warm tone) qualify -- previously only negative reviews did, so
    a genuinely good review was silently auto-closed with no draft and no
    way to see or reply to it. Capped separately (not combined) so a flood
    of 5-star reviews can't crowd out negative-review coverage, which
    matters more. Excludes unrated platforms (Instagram has no star rating
    at all, so there's no signal to pick a tone from)."""
    negative = [ra for ra in analyses if ra.rating and 0 < ra.rating <= 3][:MAX_DRAFT_REPLIES]
    positive = [ra for ra in analyses if ra.rating and ra.rating >= 4][:MAX_POSITIVE_DRAFT_REPLIES]
    return negative + positive


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


# ── Per-store config helpers ──

def _load_brand_voice(store_id: int, store_name: str) -> BrandVoice:
    """Load brand voice from ReputationConfig for this store."""
    try:
        from app.core.db import SessionLocal, ReputationConfig
        with SessionLocal() as db:
            rc = db.query(ReputationConfig).filter(ReputationConfig.store_id == store_id).first()
        if rc:
            return BrandVoice(
                name=store_name,
                tone=rc.brand_voice_tone or DEFAULT_BRAND_VOICE,
                never_say=rc.brand_voice_never_say or [],
            )
    except Exception as exc:
        logger.warning("Could not load brand voice for store %d: %s", store_id, exc)
    return BrandVoice(name=store_name, tone=DEFAULT_BRAND_VOICE)


# ── Helpers ──

_PLATFORM_NAMES = {
    "google_maps": "Google Maps",
    "instagram": "Instagram",
}


def _platform_label(pending: dict) -> str:
    platform = _PLATFORM_NAMES.get(pending.get("source_platform"), "the review site")
    url = pending.get("source_url")
    return f"{platform} ({url})" if url else platform


def _format_pending(pending: dict) -> str:
    summary = pending["ai_summary"]
    rating = pending.get("rating")
    stars = "⭐" * int(rating) if rating else ""
    rating_str = f"{stars} {rating}/5" if rating is not None else "no rating"
    excerpt = (pending.get("content_text") or "")[:150]
    draft = summary.get("draft_reply", "")
    return (
        f"*{rating_str}*\n"
        f"_{excerpt}_\n\n"
        f"*Suggested reply:*\n{draft}\n\n"
        "*POST* - mark as replied (post it on the platform yourself first)\n"
        "*EDIT <text>* - revise\n"
        "*IGNORE* - skip\n"
        "*DONE* - exit"
    )


# ── WhatsApp owner reply handler ──

def process_reputation_owner_reply(from_phone: str, body: str, store_id: int | None = None) -> str:
    """Handle a reputation-related WhatsApp message from an owner/staff member.

    store_id must be passed by the gateway (derived from the Twilio To field).
    Returns the reply string; the gateway handles sending it back via TwiML.
    """
    from app.core.db import SessionLocal, Store, VenueConfig
    from app.review_sources import db as review_db

    if store_id is None:
        return "Internal error: store could not be determined."

    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        vc = db.query(VenueConfig).filter(VenueConfig.store_id == store_id).first()
        if store:
            db.expunge(store)
        if vc:
            db.expunge(vc)

    if not store:
        return "Internal error: store not found."

    store_name = (vc.venue_name if vc else None) or store.name

    text = body.strip()
    text_lower = text.lower()
    cmd = text_lower.split()[0] if text_lower else ""
    logger.info("reputation: store=%d cmd=%s from=%s", store_id, cmd, from_phone)

    if text_lower.startswith("done") or text_lower.startswith("exit"):
        return f"*{store_name}* - Review session ended. Send *CHECK* anytime to resume."

    if text_lower.startswith("post"):
        finding = review_db.get_pending_finding(store_id)
        if not finding:
            return f"*{store_name}* - No pending review drafts right now."
        summary = finding["ai_summary"]
        summary["status"] = "posted"
        review_db.update_finding_summary(finding["id"], summary)
        draft = summary.get("draft_reply", "")
        # We can't publish this for you -- there's no write access to Google
        # Maps / Instagram review replies. This only marks the draft as
        # handled on our side; the owner still has to paste it on the
        # actual platform themselves.
        reply = (
            f"*{store_name}* - Marked as replied ✅\n\n"
            f"Copy this and post it on *{_platform_label(finding)}*:\n\n"
            f"_{draft}_"
        )
        nxt = review_db.get_pending_finding(store_id)
        return reply + ("\n\n" + _format_pending(nxt) if nxt else "\n\nNo more pending reviews. Send *DONE* to exit.")

    if text_lower.startswith("edit"):
        new_draft = text[4:].strip()
        if not new_draft:
            return f"*{store_name}* - Send *EDIT <your new message>* to revise the draft."
        finding = review_db.get_pending_finding(store_id)
        if not finding:
            return f"*{store_name}* - No pending draft found to edit."
        summary = finding["ai_summary"]
        summary["draft_reply"] = new_draft
        review_db.update_finding_summary(finding["id"], summary)
        return (
            f"*{store_name}* - Draft updated ✏️\n\n_{new_draft}_\n\n"
            "*POST* - mark as replied (post it on the platform yourself first)\n"
            "*IGNORE* - skip\n"
            "*DONE* - exit"
        )

    if text_lower.startswith("ignore"):
        finding = review_db.get_pending_finding(store_id)
        if not finding:
            return f"*{store_name}* - No pending draft to ignore."
        summary = finding["ai_summary"]
        summary["status"] = "ignored"
        review_db.update_finding_summary(finding["id"], summary)
        nxt = review_db.get_pending_finding(store_id)
        reply = f"*{store_name}* - Skipped."
        return reply + ("\n\n" + _format_pending(nxt) if nxt else "\n\nNo more pending reviews. Send *DONE* to exit.")

    if any(kw in text_lower for kw in ("check", "scrape", "crawl", "sync")):
        return _check_reviews(store_id, store_name)

    return _chat_about_reviews(store_id, store_name, text)


def check_reputation_cache(store_id: int, store_name: str) -> tuple[bool, str]:
    """Return (True, text) if a completed check exists within REPUTATION_CACHE_HOURS, else (False, '')."""
    from datetime import datetime, timedelta
    from app.review_sources import db as review_db
    from app.core.db import SessionLocal, ScoutRun as Run

    cutoff = datetime.utcnow() - timedelta(hours=REPUTATION_CACHE_HOURS)
    with SessionLocal() as db:
        cached = db.query(Run).filter(
            Run.store_id == store_id,
            Run.command == "whatsapp_check",
            Run.status == "ok",
            Run.finished_at >= cutoff,
        ).order_by(Run.finished_at.desc()).first()
        if cached:
            db.expunge(cached)

    if not cached:
        return False, ""

    age_min = int((datetime.utcnow() - cached.finished_at).total_seconds() / 60)
    age_str = f"{age_min} min ago" if age_min > 0 else "just now"
    logger.info("reputation.cache_hit: store=%d age_min=%d", store_id, age_min)

    pending = review_db.get_pending_finding(store_id)
    if pending:
        text = (
            f"*{store_name}* - Reviews cached ({age_str}). Pending reply:\n\n"
            + _format_pending(pending)
        )
    else:
        text = f"*{store_name}* - Reviews up to date ({age_str}). No pending replies."

    return True, text


def _check_reviews(store_id: int, store_name: str) -> str:
    from datetime import datetime, timedelta
    from app.review_sources.pipeline import run_pipeline
    from app.review_sources import db as review_db
    from app.review_sources.normalizer import to_review_model
    from app.core.llm import get_client
    from app.core.db import SessionLocal, ScoutRun as Run

    # In-flight guard: if a scrape is already running for this store, wait for it
    cutoff_inflight = datetime.utcnow() - timedelta(minutes=10)
    with SessionLocal() as db:
        in_flight = db.query(Run).filter(
            Run.store_id == store_id,
            Run.command == "whatsapp_check",
            Run.status == "running",
            Run.started_at >= cutoff_inflight,
        ).first()
    if in_flight:
        return f"*{store_name}* - Review scrape already in progress. Results coming shortly 🔍"

    # Cache: if a review check completed recently, skip Apify and read from DB
    cutoff = datetime.utcnow() - timedelta(hours=REPUTATION_CACHE_HOURS)
    with SessionLocal() as db:
        cached = db.query(Run).filter(
            Run.store_id == store_id,
            Run.command == "whatsapp_check",
            Run.status == "ok",
            Run.finished_at >= cutoff,
        ).order_by(Run.finished_at.desc()).first()
        if cached:
            db.expunge(cached)

    if cached:
        age_min = int((datetime.utcnow() - cached.finished_at).total_seconds() / 60)
        age_str = f"{age_min} min ago" if age_min > 0 else "just now"
        logger.info("reputation.check: store=%d cache_hit age_min=%d", store_id, age_min)
        pending = review_db.get_pending_finding(store_id)
        if pending:
            return (
                f"*{store_name}* - Reviews cached ({age_str}). Pending reply:\n\n"
                + _format_pending(pending)
            )
        return f"*{store_name}* - Reviews up to date ({age_str}). No pending replies."

    # Mark run as "running" before Apify so in-flight guard can detect it
    run_id = review_db.save_run(store_id, "whatsapp_check")

    logger.info("reputation.check: store=%d scraping_reviews", store_id)
    try:
        raw_reviews, sources_ok, sources_failed = run_pipeline(store_id)
    except Exception as exc:
        logger.error("reputation.check: store=%d pipeline_failed error=%s", store_id, exc)
        review_db.update_run(run_id, "error", [], [], 0)
        return f"*{store_name}* - Review check failed: {exc}"

    logger.info(
        "reputation.check: store=%d reviews_found=%d sources_ok=%s sources_failed=%s",
        store_id, len(raw_reviews), sources_ok, sources_failed,
    )
    if not raw_reviews:
        status = "ok" if not sources_failed else ("partial" if sources_ok else "error")
        review_db.update_run(run_id, status, sources_ok, sources_failed, 0)
        return f"*{store_name}* - No new reviews found across all platforms."

    # Cap to most recent MAX_REVIEWS_PER_CHECK reviews
    raw_reviews = sorted(
        raw_reviews,
        key=lambda r: r.get("posted_at") or r.get("date") or "",
        reverse=True,
    )[:MAX_REVIEWS_PER_CHECK]
    logger.info("reputation.check: store=%d capped_to=%d", store_id, len(raw_reviews))

    client = get_client()

    # Load brand voice from per-store config
    brand = _load_brand_voice(store_id, store_name)

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

    needs_reply = select_reviews_needing_reply([ra for _, ra in analyses])
    draft_replies(needs_reply, client, venue_name=store_name, brand_voice=brand)
    reply_map = {ra.review_id: ra for ra in needs_reply}

    new_count = 0
    for raw, ra in analyses:
        drafted = reply_map.get(ra.review_id)
        is_actionable = drafted is not None
        ai_summary = {
            "status": "pending" if is_actionable else "auto_closed",
            "sentiment": ra.sentiment,
            "issue_class": ra.issue_class,
            "draft_reply": drafted.draft_reply if drafted else "",
            "correlation": {
                "estimated_date": ra.correlation.estimated_date,
                "matched_staff_name": ra.correlation.matched_staff_name,
                "confidence": ra.correlation.confidence,
            },
        }
        if review_db.save_review_finding(store_id, run_id, store_name, raw, ai_summary):
            new_count += 1

    status = "ok" if not sources_failed else ("partial" if sources_ok else "error")
    review_db.update_run(run_id, status, sources_ok, sources_failed, new_count)

    if new_count == 0:
        return f"*{store_name}* - No new reviews found. All up to date."

    pending = review_db.get_pending_finding(store_id)
    if pending:
        return (
            f"*{store_name}* - Processed {new_count} new review{'s' if new_count != 1 else ''}. "
            f"Latest pending:\n\n" + _format_pending(pending)
        )

    return f"*{store_name}* - Processed {new_count} new review{'s' if new_count != 1 else ''}. No negative reviews to action."


def _active_store_ids() -> list[int]:
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        return [s.id for s in db.query(Store).all()]


def run_reputation_check_all() -> None:
    """Scheduled job (see scripts/run_server.py) -- runs a real review check
    for every store on a cadence matched to REPUTATION_CACHE_HOURS, so a
    staff member's own "check" command almost always lands on a warm cache
    instead of waiting on a live Apify scrape. Reuses _check_reviews()
    exactly as the real WhatsApp path does; the returned text is just
    logged, never sent -- this only needs to populate the cache, not notify
    anyone. _check_reviews' own in-flight guard and cache check make this
    safe to call even if a staff member's own check overlaps with a
    scheduled run."""
    from app.core.db import SessionLocal, Store

    for store_id in _active_store_ids():
        try:
            with SessionLocal() as db:
                store = db.query(Store).filter(Store.id == store_id).first()
                store_name = store.name if store else "restaurant"
            result = _check_reviews(store_id, store_name)
            logger.info("reputation.cron_check: store=%d result=%r", store_id, result[:120])
        except Exception as exc:
            logger.error("reputation.cron_check: store=%d failed: %s", store_id, exc)


def _chat_about_reviews(store_id: int, store_name: str, text: str) -> str:
    from app.review_sources import db as review_db
    from app.core.llm import get_client, get_model, nothink_kwargs

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
        recent_ctx += "(None, type CHECK to scrape new reviews)\n"

    system = (
        f"You assist the owner of '{store_name}' with review management on WhatsApp. "
        "Be brief, direct and conversational.\n"
        "Commands: *POST* (mark draft as replied -- owner still has to post it "
        "on the actual platform themselves, we can't publish it for them), "
        "*EDIT <text>* (revise draft), "
        "*IGNORE* (skip), *CHECK* (scrape new reviews).\n\n"
        "WhatsApp format: no markdown, no em-dashes (use a comma or colon instead), "
        "no emojis, use *word* for bold, short paragraphs.\n\n"
        f"{pending_ctx}\n{recent_ctx}"
    )

    try:
        client = get_client()
        resp = client.chat.completions.create(
            timeout=20.0,
            model=get_model(),
            max_tokens=500,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            **nothink_kwargs(get_model()),
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("Reputation chat error: %s", exc)
        return (
            f"*{store_name}* - Reviews\n\n"
            "*POST* - mark draft as replied (post it on the platform yourself first)\n"
            "*EDIT <text>* - revise the draft\n"
            "*IGNORE* - skip this review\n"
            "*CHECK* - scrape new reviews"
        )
