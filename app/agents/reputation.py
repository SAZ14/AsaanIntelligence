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
# Every review is now processed and stored (not just the newest 50) -- this
# is a defensive safety ceiling on genuinely NEW reviews in a single check,
# not a routine cap. Dedup against the DB happens before this and before any
# LLM call, so steady-state volume per check is normally just the delta
# since the last check; this only matters for a large first-time catch-up
# run against a store with a big backlog.
MAX_NEW_REVIEWS_PER_CHECK = 500
MAX_DRAFT_REPLIES = 5
MAX_POSITIVE_DRAFT_REPLIES = 5
# Matches the cron cadence in scripts/run_server.py (3x/day, ~8h apart) --
# by the time a staff member checks, a scheduled run should always have
# happened within this window, so their check hits cache instantly instead
# of waiting on a live Apify scrape.
REPUTATION_CACHE_HOURS = 8

# TTL on the atomic Redis lock (_check_reviews' in-flight guard) that
# decides whether a review scrape is already running for a store. A real
# scrape has been observed to complete in ~5-6 minutes; this stays well
# above that so a genuinely still-running scrape is never mistaken for
# stale/orphaned, while still releasing itself as a safety net if a
# crashed process never reaches the `finally: release_lock`.
REPUTATION_RUN_LOCK_MINUTES = 20

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
    # False for content that isn't actually feedback about the food/
    # service/experience -- a question ("do you deliver?"), a well-wish
    # ("good luck!"), a business inquiry ("collaboration"), or an
    # off-topic/spam remark. Confirmed live: Instagram comments (casual
    # social replies, not a dedicated review system like Google Maps)
    # regularly include these mixed in with genuine feedback, and they
    # were getting a sentiment tag and counted as real reviews anyway.
    # Defaults True -- only the LLM classification path (recent/unrated
    # items) actually evaluates this; rule-based classification of older
    # RATED reviews skips it, since a star rating means the item went
    # through the platform's own review-submission flow already.
    is_review: bool = True


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
            "Classify each review. Reply with one line per review: "
            "N. issue_class,sentiment,is_review\n"
            f"Issue classes: {', '.join(ISSUE_CLASSES)}\n"
            "Sentiments: positive, negative, neutral, mixed\n"
            "is_review is 'yes' if this is genuine feedback about the food, "
            "service, or experience -- 'no' if it's actually a question "
            "('do you deliver?'), a well-wish ('good luck!', 'welcome back'), "
            "a business inquiry ('collaboration'), or an off-topic/unrelated "
            "remark. A short-but-genuine reaction ('yumm', 'so good') is "
            "still is_review=yes -- only mark 'no' when it isn't feedback "
            "about the business AT ALL, not just because it's brief.\n\n"
            + "\n".join(lines)
        )
        # Confirmed live: the model doesn't reliably substitute the "N."
        # line-number placeholder with the actual index -- it sometimes
        # echoes the literal "N." on every line, or even echoes the
        # instruction template itself as a stray line ("N. issue_class,
        # sentiment,is_review"), which can make an ENTIRE batch parse to
        # zero valid lines. Trusting the parsed number as an array index
        # meant that failure mode silently left every review at its
        # default (sentiment=neutral, is_review=True) with no error and
        # no retry -- wrong classifications got saved permanently. Fixed
        # three ways: (1) match POSITIONALLY -- the Kth valid line maps
        # to batch[K], regardless of what number prefix the model
        # actually wrote, so "N."/"1."/wrong numbers all still work;
        # (2) only accept a line whose three values are all real
        # ISSUE_CLASSES/sentiments/yes-no -- an echoed instruction line
        # fails this validation and gets skipped rather than silently
        # consuming a slot and shifting every later item off by one;
        # (3) retry once if a whole batch comes back with zero matches,
        # since a second attempt at the same non-deterministic call has
        # reliably produced well-formed output when this was tested live.
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    timeout=60.0,
                    model=get_model(),
                    max_tokens=CLASSIFIER_BATCH_SIZE * 16,
                    messages=[{"role": "user", "content": prompt}],
                    **nothink_kwargs(get_model()),
                )
                output = resp.choices[0].message.content.strip()
                matched = 0
                for line in output.split("\n"):
                    m = re.match(r"(?:\d+|N)\.\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*$", line.strip())
                    if not m:
                        continue
                    issue = m.group(1).strip().lower()
                    sent = m.group(2).strip().lower()
                    is_review = m.group(3).strip().lower()
                    if issue not in ISSUE_CLASSES or sent not in ("positive", "negative", "neutral", "mixed") \
                            or is_review not in ("yes", "no"):
                        continue
                    if matched >= len(batch):
                        break
                    batch[matched].issue_class = issue
                    batch[matched].sentiment = sent
                    batch[matched].is_review = is_review == "yes"
                    matched += 1
                if matched == 0 and attempt == 0:
                    logger.warning("Batch classify: 0/%d matched, retrying once", len(batch))
                    continue
                if matched < len(batch):
                    logger.warning(
                        "Batch classify: only %d/%d reviews got a valid classification line",
                        matched, len(batch),
                    )
                break
            except Exception as exc:
                logger.warning("Batch classify failed: %s", exc)
                break

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


def _review_platform(source: str) -> str:
    source = source or ""
    if source.startswith("Google"):
        return "google_maps"
    if source.startswith("Instagram"):
        return "instagram"
    return "other"


def cap_reviews_balanced(raw_reviews: list[dict], max_total: int) -> list[dict]:
    """Cap scraped reviews to max_total, balanced across platforms instead
    of a flat global recency sort.

    Confirmed live: Instagram has no star ratings at all, so a review
    from there can never be classified as negative or positive by
    select_reviews_needing_reply (both buckets require a real rating) --
    Google Maps is the only source that can ever produce a drafted reply
    or a pending-queue item. A pure "most recent N overall" cap doesn't
    protect that: a burst of recent Instagram comments crowded out every
    single Google Maps review from a real 50-review batch, so despite
    real (possibly negative or positive) Google Maps reviews existing,
    none of them made it into the capped batch at all -- the report said
    "no negative reviews" not because there weren't any, but because
    none were even looked at.

    Splits the budget evenly across whichever platforms are present
    (each internally sorted by recency), then backfills any leftover
    budget with whatever's most recent overall -- so a platform with
    fewer items than its even share doesn't waste budget, and one with
    a lot doesn't get capped below its share unless truly outnumbered.
    """
    from collections import defaultdict

    by_platform: dict[str, list[dict]] = defaultdict(list)
    for r in raw_reviews:
        by_platform[_review_platform(r.get("source", ""))].append(r)

    for items in by_platform.values():
        items.sort(key=lambda r: r.get("posted_at") or r.get("date") or "", reverse=True)

    platforms = list(by_platform.keys())
    if not platforms:
        return []
    per_platform = max(1, max_total // len(platforms))

    selected: list[dict] = []
    for platform in platforms:
        selected.extend(by_platform[platform][:per_platform])

    if len(selected) < max_total:
        leftover: list[dict] = []
        for platform in platforms:
            leftover.extend(by_platform[platform][per_platform:])
        leftover.sort(key=lambda r: r.get("posted_at") or r.get("date") or "", reverse=True)
        selected.extend(leftover[: max_total - len(selected)])

    return selected[:max_total]


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
    is_instagram = (pending.get("source_platform") or "").startswith("Instagram")
    if rating is not None:
        rating_str = f"{stars} {rating}/5"
    elif is_instagram:
        # Instagram content never has a star rating -- not a data gap,
        # nothing to flag here the way a genuinely missing Google Maps
        # rating would be.
        rating_str = "Instagram"
    else:
        rating_str = "no rating"
    excerpt = _excerpt(pending.get("content_text") or "", 200)
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

    if text_lower == "next":
        state = _get_review_page_state(store_id, from_phone)
        if not state:
            return (
                f"*{store_name}* - Nothing to continue. Try *positive reviews*, "
                "*negative reviews*, or *all reviews*."
            )
        return _list_reviews_page(
            store_id, store_name, from_phone,
            state.get("sentiment"), state.get("status"), state.get("offset", 0),
            state.get("days_back"),
        )

    # Real commands (not just free-form chat) -- also what the LLM router
    # in gateway/internal.py routes matching natural language to
    # (reputation/positive, reputation/negative, reputation/reviews), so
    # this fires the same way whether the user typed the phrase directly
    # or the router normalized something like "show me the good reviews"
    # down to it.
    if text_lower in _POSITIVE_REVIEW_TRIGGERS:
        return _list_reviews_page(store_id, store_name, from_phone, "positive", None)
    if text_lower in _NEGATIVE_REVIEW_TRIGGERS:
        return _list_reviews_page(store_id, store_name, from_phone, "negative", None)
    if text_lower in _ALL_REVIEW_TRIGGERS:
        return _list_reviews_page(store_id, store_name, from_phone, None, None)

    # Anything else natural-language-shaped that still asks to see a
    # filtered set ("what did we ignore", "what have we posted", "show
    # me last week's positive reviews only") -- answered from the same
    # direct, deterministic DB query rather than the general chat LLM
    # guessing from a small recent-reviews sample. See
    # _classify_review_query's docstring for why. days_back is detected
    # separately from sentiment/status (_detect_time_range, a genuinely
    # different question -- "is there a time range" vs "is this a list
    # request at all") and only applied when this IS a list request, so
    # a time phrase alone ("what happened last week") without any
    # sentiment/status signal still falls through to general chat rather
    # than being misread as a list request.
    sentiment_filter, status_filter = _classify_review_query(text)
    if sentiment_filter or status_filter:
        days_back = _detect_time_range(text)
        return _list_reviews_page(store_id, store_name, from_phone, sentiment_filter, status_filter, days_back=days_back)

    return _chat_about_reviews(store_id, store_name, text)


def _reputation_cache_key(store_id: int) -> str:
    return f"reputation:last_check:{store_id}"


def _reputation_live_lock_key(store_id: int) -> str:
    return f"reputation:live_lock:{store_id}"


def _get_reputation_last_check(store_id: int) -> datetime | None:
    """Timestamp of the most recent completed ("ok") review check within
    REPUTATION_CACHE_HOURS, or None if stale/missing.

    Checks Redis first (app/core/cache.py -- same connection already used
    for rate limits/cooldowns/the job queue) so the common case (a check
    shortly after another check or a cron poll) is a single fast round-trip
    instead of a Postgres query. Postgres remains the source of truth: on a
    Redis miss (cold start, restart, key eviction -- cache.py fails open
    the same way everywhere else in this codebase), falls back to the same
    query this used to run unconditionally, and repopulates Redis with
    whatever's left of the freshness window so the next call hits the fast
    path again. This one helper replaces what used to be two separate,
    near-identical Postgres queries (check_reputation_cache and
    _check_reviews each had their own copy)."""
    from app.core import cache as _cache

    key = _reputation_cache_key(store_id)
    cached = _cache.get(key)
    if cached is not None:
        try:
            return datetime.fromisoformat(cached["finished_at"])
        except Exception:
            pass  # malformed cache entry -- fall through to Postgres

    from app.core.db import SessionLocal, ScoutRun as Run

    cutoff = datetime.utcnow() - timedelta(hours=REPUTATION_CACHE_HOURS)
    with SessionLocal() as db:
        run = db.query(Run).filter(
            Run.store_id == store_id,
            Run.command == "whatsapp_check",
            Run.status == "ok",
            Run.finished_at >= cutoff,
        ).order_by(Run.finished_at.desc()).first()
        finished_at = run.finished_at if run else None

    if finished_at is not None:
        remaining = REPUTATION_CACHE_HOURS * 3600 - int((datetime.utcnow() - finished_at).total_seconds())
        if remaining > 0:
            _cache.set(key, {"finished_at": finished_at.isoformat()}, ttl=remaining)
    return finished_at


def _get_reputation_last_check_unbounded(store_id: int) -> datetime | None:
    """Like _get_reputation_last_check but with no REPUTATION_CACHE_HOURS
    cutoff -- used only when the Apify circuit breaker is open, to serve
    whatever's on record however old rather than nothing at all."""
    from app.core.db import SessionLocal, ScoutRun as Run
    with SessionLocal() as db:
        run = db.query(Run).filter(
            Run.store_id == store_id,
            Run.command == "whatsapp_check",
            Run.status == "ok",
        ).order_by(Run.finished_at.desc()).first()
        return run.finished_at if run else None


def _mark_reputation_checked(store_id: int, finished_at: datetime) -> None:
    from app.core import cache as _cache
    _cache.set(
        _reputation_cache_key(store_id),
        {"finished_at": finished_at.isoformat()},
        ttl=REPUTATION_CACHE_HOURS * 3600,
    )


def check_reputation_cache(store_id: int, store_name: str) -> tuple[bool, str]:
    """Return (True, text) if a completed check exists within REPUTATION_CACHE_HOURS, else (False, '')."""
    from app.review_sources import db as review_db

    finished_at = _get_reputation_last_check(store_id)
    if finished_at is None:
        return False, ""

    age_min = int((datetime.utcnow() - finished_at).total_seconds() / 60)
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
    from datetime import datetime
    from app.review_sources.pipeline import run_pipeline
    from app.review_sources import db as review_db
    from app.review_sources.normalizer import to_review_model
    from app.core.llm import get_client
    from app.core import cache as _cache

    # Cache: if a review check completed recently, skip Apify and read from
    # DB. Checked before the in-flight lock below so the common case (cache
    # still warm) never touches Redis's lock machinery at all.
    cache_finished_at = _get_reputation_last_check(store_id)
    if cache_finished_at is not None:
        age_min = int((datetime.utcnow() - cache_finished_at).total_seconds() / 60)
        age_str = f"{age_min} min ago" if age_min > 0 else "just now"
        logger.info("reputation.check: store=%d cache_hit age_min=%d", store_id, age_min)
        pending = review_db.get_pending_finding(store_id)
        if pending:
            return (
                f"*{store_name}* - Reviews cached ({age_str}). Pending reply:\n\n"
                + _format_pending(pending)
            )
        return f"*{store_name}* - Reviews up to date ({age_str}). No pending replies."

    # Circuit breaker open (app.core.apify_guard, shared with scout): don't
    # attempt a live scrape at all, even for this manual "check" -- confirmed
    # live this needs to apply here too, not just the scheduled cron. Serve
    # whatever's on record however old rather than one more guaranteed-fail
    # Apify call against an account that's already rate-limited.
    from app.core import apify_guard
    if apify_guard.breaker_open():
        last_known = _get_reputation_last_check_unbounded(store_id)
        if last_known is not None:
            age_min = int((datetime.utcnow() - last_known).total_seconds() / 60)
            age_str = f"{age_min} min ago" if age_min > 0 else "just now"
            pending = review_db.get_pending_finding(store_id)
            note = "Data provider is temporarily rate-limited, showing the last known status"
            if pending:
                return (
                    f"*{store_name}* - {note} ({age_str}). Pending reply:\n\n"
                    + _format_pending(pending)
                )
            return f"*{store_name}* - {note} ({age_str}). No pending replies."
        return (
            f"*{store_name}* - Review check unavailable (data provider usage limit "
            f"reached) and no previous check is on record yet. Please try again later."
        )

    # In-flight guard: atomic Redis lock (SET NX -- app/core/cache.py, same
    # connection as rate limits/cooldowns/the job queue), not a Postgres
    # SELECT-then-INSERT. The two-step version is racy: two callers (the
    # cron poll and a staff message arriving moments apart, both having
    # just seen the same "stale" cache result above) could both decide
    # independently to proceed and both start a scrape. try_lock is
    # atomic -- only one caller can ever hold this key, so at most one
    # scrape ever actually runs for this store at a time.
    lock_key = _reputation_live_lock_key(store_id)
    if not _cache.try_lock(lock_key, ttl_seconds=REPUTATION_RUN_LOCK_MINUTES * 60):
        return f"*{store_name}* - Review scrape already in progress. Results coming shortly 🔍"

    try:
        run_id = review_db.save_run(store_id, "whatsapp_check")

        logger.info("reputation.check: store=%d scraping_reviews", store_id)
        try:
            raw_reviews, sources_ok, sources_failed, quota_exceeded = run_pipeline(store_id)
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
            from app.core import apify_guard
            if status == "ok":
                _mark_reputation_checked(store_id, datetime.utcnow())
                apify_guard.record_success()
            elif quota_exceeded:
                # Total failure specifically because Apify's account-level
                # quota is exhausted -- _mark_reputation_checked is
                # deliberately NOT called (status != "ok"), so the existing
                # "last known good check" timestamp is untouched and future
                # cache reads keep serving it, however old, rather than
                # this failed attempt (confirmed live this needs saying
                # explicitly rather than the generic "no new reviews"
                # message, which reads as a successful up-to-date check).
                apify_guard.record_quota_failure("reputation")
                return (
                    f"*{store_name}* - Review check failed (data provider usage limit "
                    f"reached). Your last known review status is unchanged."
                )
            return f"*{store_name}* - No new reviews found across all platforms."

        # Dedup against the DB BEFORE any classification or drafting -- the
        # only thing that makes "process everything scraped, not just the
        # newest 50" both correct and cheap. A review already seen in a
        # previous run is skipped entirely here: never reclassified, never
        # redrafted, and save_review_finding() leaves its existing workflow
        # status (pending/posted/ignored) and draft completely untouched.
        # Steady-state cost per check is therefore just the delta of
        # genuinely new reviews since the last check, not the full scrape.
        hashes = [r.get("hash", "") for r in raw_reviews if r.get("hash")]
        already_seen = review_db.existing_content_hashes(store_id, hashes)
        new_raw_reviews = [r for r in raw_reviews if r.get("hash") not in already_seen]
        logger.info(
            "reputation.check: store=%d scraped=%d already_seen=%d new=%d",
            store_id, len(raw_reviews), len(already_seen), len(new_raw_reviews),
        )

        if not new_raw_reviews:
            status = "ok" if not sources_failed else ("partial" if sources_ok else "error")
            review_db.update_run(run_id, status, sources_ok, sources_failed, 0)
            if status == "ok":
                _mark_reputation_checked(store_id, datetime.utcnow())
                from app.core import apify_guard
                apify_guard.record_success()
            return f"*{store_name}* - No new reviews found. All up to date."

        # Defensive safety ceiling, not a routine cap -- see MAX_NEW_REVIEWS_PER_CHECK.
        raw_reviews = cap_reviews_balanced(new_raw_reviews, MAX_NEW_REVIEWS_PER_CHECK)
        logger.info("reputation.check: store=%d processing=%d", store_id, len(raw_reviews))

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

        # Drop non-feedback content (questions, well-wishes, business
        # inquiries, off-topic remarks) before drafting replies or saving
        # -- no point drafting a business reply to "do you deliver?", and
        # no point storing it as if it were a real review affecting
        # sentiment counts. Confirmed live: Instagram comments regularly
        # mix genuine feedback with this kind of noise (unlike Google
        # Maps, a dedicated review system where every entry is already a
        # deliberate review submission).
        skipped_non_review = sum(1 for _, ra in analyses if not ra.is_review)
        if skipped_non_review:
            logger.info("reputation.check: store=%d skipped_non_review=%d", store_id, skipped_non_review)
        analyses = [(raw, ra) for raw, ra in analyses if ra.is_review]

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
            if review_db.save_review_finding(store_id, run_id, store_name, raw, ai_summary) == "new":
                new_count += 1

        status = "ok" if not sources_failed else ("partial" if sources_ok else "error")
        review_db.update_run(run_id, status, sources_ok, sources_failed, new_count)
        if status == "ok":
            _mark_reputation_checked(store_id, datetime.utcnow())
            from app.core import apify_guard
            apify_guard.record_success()

        if new_count == 0:
            return f"*{store_name}* - No new reviews found. All up to date."

        pending = review_db.get_pending_finding(store_id)
        if pending:
            return (
                f"*{store_name}* - Processed {new_count} new review{'s' if new_count != 1 else ''}. "
                f"Latest pending:\n\n" + _format_pending(pending)
            )

        return f"*{store_name}* - Processed {new_count} new review{'s' if new_count != 1 else ''}. Nothing needs a reply right now."
    finally:
        _cache.release_lock(lock_key)


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
    scheduled run.

    Also checks the shared Apify circuit breaker (app.core.apify_guard,
    tripped after 3 consecutive account-level quota failures, shared with
    scout's cron) before touching any store -- confirmed live this
    matters: without it, a 1-minute cron interval means an Apify outage
    gets retried up to 1440 times a day, which risks Apify rate-limiting
    or banning the account outright."""
    from app.core.db import SessionLocal, Store
    from app.core import apify_guard

    if apify_guard.breaker_open():
        logger.warning("reputation.cron: apify breaker open -- skipping all stores this cycle")
        return

    for store_id in _active_store_ids():
        try:
            with SessionLocal() as db:
                store = db.query(Store).filter(Store.id == store_id).first()
                store_name = store.name if store else "restaurant"
            result = _check_reviews(store_id, store_name)
            logger.info("reputation.cron_check: store=%d result=%r", store_id, result[:120])
        except Exception as exc:
            logger.error("reputation.cron_check: store=%d failed: %s", store_id, exc)


_SENTIMENT_FILTERS = ("positive", "negative", "neutral")
_STATUS_FILTERS = ("pending", "posted", "ignored")

# Exact/near-exact trigger phrases for the review-listing commands.
# Matched both against what a staff member literally types AND against
# the canonical command keyword the LLM router (gateway/internal.py)
# normalizes natural language down to (e.g. "show me the good reviews"
# -> agent=reputation command=positive -> body_to_send="positive").
_POSITIVE_REVIEW_TRIGGERS = {
    "positive", "positive reviews", "good reviews", "show positive reviews",
    "show me positive reviews", "list positive reviews",
}
_NEGATIVE_REVIEW_TRIGGERS = {
    "negative", "negative reviews", "bad reviews", "show negative reviews",
    "show me negative reviews", "list negative reviews",
}
_ALL_REVIEW_TRIGGERS = {
    "reviews", "all reviews", "list reviews", "show reviews",
    "show all reviews", "list all reviews",
}


def _classify_review_query(text: str) -> tuple[str | None, str | None]:
    """(sentiment, status) filter this message is asking to see/list, or
    (None, None) if it isn't a list request at all (general question,
    small talk, etc -- falls through to _chat_about_reviews instead).

    Deliberately a small, separate LLM classification call (same pattern
    as the staff router's own intent classifier in gateway/internal.py)
    rather than folding this into _chat_about_reviews' free-form answer:
    _chat_about_reviews only ever sees a handful of recent reviews, so
    asking it to accurately filter/count/list across everything scraped
    (now unbounded, previously 300+) isn't reliable -- LLMs are prone to
    miscounting or missing items over a long list embedded in a prompt.
    Classifying intent here and then querying the DB directly (list_reviews)
    keeps the actual filtering deterministic and exact."""
    try:
        from app.core.llm import get_client, get_fast_model, nothink_kwargs
        client = get_client()
        fast_model = get_fast_model()
        resp = client.chat.completions.create(
            model=fast_model,
            messages=[
                {"role": "system", "content": (
                    "Classify whether this WhatsApp message from a restaurant owner is "
                    "asking to SEE or LIST a specific set of reviews, and if so which "
                    "filter. Reply with EXACTLY one line in the format sentiment,status "
                    "-- using 'none' for whichever axis isn't specified.\n"
                    "sentiment is one of: positive, negative, neutral, none\n"
                    "status is one of: pending, posted, ignored, none -- 'ignored' also "
                    "covers 'skip'/'skipped'/'what did we skip'\n"
                    "If the message is NOT asking to see/list reviews (a general "
                    "question, a command, small talk), reply exactly: none,none\n\n"
                    "Examples:\n"
                    "'show me positive reviews' -> positive,none\n"
                    "'what are the bad ones' -> negative,none\n"
                    "'list ignored reviews' -> none,ignored\n"
                    "'what did we skip' -> none,ignored\n"
                    "'what did we already post' -> none,posted\n"
                    "'how is our rating trending' -> none,none\n"
                    "'check reviews' -> none,none"
                )},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=10,
            timeout=8.0,
            **nothink_kwargs(fast_model),
        )
        result = resp.choices[0].message.content.strip().lower()
        sentiment, _, status = result.partition(",")
        sentiment, status = sentiment.strip(), status.strip()
        return (
            sentiment if sentiment in _SENTIMENT_FILTERS else None,
            status if status in _STATUS_FILTERS else None,
        )
    except Exception as exc:
        logger.warning("reputation._classify_review_query: failed (%s) -- keyword fallback", exc)
        return _classify_review_query_fallback(text)


def _classify_review_query_fallback(text: str) -> tuple[str | None, str | None]:
    lower = text.lower()
    sentiment = None
    if re.search(r"\b(positive|good|great|happy)\b", lower):
        sentiment = "positive"
    elif re.search(r"\b(negative|bad|worst|complain\w*|unhapp\w*)\b", lower):
        sentiment = "negative"
    status = None
    if re.search(r"\b(ignored?|skipp?ed?)\b", lower):
        status = "ignored"
    elif re.search(r"\bposted?\b", lower):
        status = "posted"
    elif re.search(r"\bpending\b", lower):
        status = "pending"
    return sentiment, status


def _excerpt(text: str, limit: int) -> str:
    """Truncate for display, but say so -- confirmed live: the old hard
    cut with no marker made a review stop mid-sentence with no
    indication anything was cut, reading as missing/corrupted data
    (the actual stored text was always complete; this was purely a
    display issue, not an Apify scraping gap)."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _filter_label(sentiment: str | None, status: str | None, days_back: int | None = None) -> str:
    parts = [p for p in (sentiment, status) if p]
    label = " ".join(parts) if parts else "all"
    if days_back:
        if days_back == 1:
            label += " (last day)"
        elif days_back == 7:
            label += " (last week)"
        elif days_back % 7 == 0:
            label += f" (last {days_back // 7} weeks)"
        else:
            label += f" (last {days_back} days)"
    return label


def _detect_sentiment_lean(text: str) -> str | None:
    """Does answering this free-form question care about POSITIVE or
    NEGATIVE reviews specifically, or neither? A different question from
    _classify_review_query above (which detects LIST requests and
    deliberately returns none for free-form questions like this one, so
    it can't be reused here) -- this exists to fix a real gap in
    search_reviews_semantic: pure topical cosine similarity matches
    subject matter, not polarity, so "complaints about the branch"
    surfaced praise ("Very nice amazing Branch") right alongside actual
    complaints, since both mention "branch". Confirmed live against
    real production review data before this existed. Feeding the
    detected lean into search_reviews_semantic's sentiment filter (which
    uses each review's already-classified ai_summary.sentiment, computed
    once during the normal review check, not re-classified here) fixes
    that by excluding the wrong-polarity matches entirely rather than
    just topically-adjacent ones."""
    try:
        from app.core.llm import get_client, get_fast_model, nothink_kwargs
        client = get_client()
        fast_model = get_fast_model()
        resp = client.chat.completions.create(
            model=fast_model,
            messages=[
                {"role": "system", "content": (
                    "A restaurant owner is asking a free-form question about their "
                    "reviews. Does answering it well specifically require POSITIVE "
                    "reviews, specifically NEGATIVE reviews, or could relevant "
                    "content be either (a neutral/general topic)?\n"
                    "Reply with EXACTLY one word: positive, negative, or none.\n"
                    "'none' means the question itself doesn't lean toward one "
                    "polarity -- use it whenever unsure, since wrongly filtering "
                    "out relevant content is worse than including a bit extra.\n\n"
                    "Examples:\n"
                    "'complaints about the branch' -> negative\n"
                    "'has anyone complained about parking' -> negative\n"
                    "'any issues with wifi' -> negative\n"
                    "'praise for the new branch' -> positive\n"
                    "'what do people love about us' -> positive\n"
                    "'what do people say about the branch' -> none\n"
                    "'how is the food quality' -> none\n"
                    "'is the wifi any good' -> none"
                )},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=5,
            timeout=8.0,
            **nothink_kwargs(fast_model),
        )
        answer = resp.choices[0].message.content.strip().lower()
        return answer if answer in ("positive", "negative") else None
    except Exception as exc:
        logger.warning("reputation._detect_sentiment_lean: failed (%s)", exc)
        return None


def _detect_time_range(text: str) -> int | None:
    """How many days back this message asks to filter reviews by (e.g.
    "last week" -> 7), or None if no time range is specified at all. A
    rolling window in CALENDAR DATES, not a precise 24h-multiple
    timestamp -- N covers today plus the previous N-1 dates (see
    list_reviews' days_back handling: post_date is always stored at
    midnight, so the cutoff is midnight-aligned too, otherwise a review
    posted "yesterday" could fall outside a naive "now minus 1 day"
    window depending on what time of day "now" happens to be, confirmed
    live). "yesterday" maps to 2 (today+yesterday), not 1 -- a rolling
    day-COUNT can't precisely express "yesterday only, excluding today"
    the way calendar boundaries could; better to include one extra day
    than to silently exclude the day actually being asked about.

    Needs "today" as context to resolve relative phrases at all, so the
    prompt includes the actual current date rather than asking the model
    to guess what "now" means."""
    if not text or not text.strip():
        return None
    try:
        from app.core.llm import get_client, get_fast_model, nothink_kwargs
        client = get_client()
        fast_model = get_fast_model()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        resp = client.chat.completions.create(
            model=fast_model,
            messages=[
                {"role": "system", "content": (
                    f"Today's date is {today}. Does this message ask for "
                    "reviews from a SPECIFIC recent time range? If so, reply "
                    "with EXACTLY one integer: how many days back that range "
                    "covers. If no time range is mentioned at all, reply "
                    "exactly: none\n\n"
                    "Examples:\n"
                    "'last week' -> 7\n"
                    "'yesterday' -> 2\n"
                    "'today' -> 1\n"
                    "'last 3 days' -> 3\n"
                    "'this month' -> 30\n"
                    "'last month' -> 30\n"
                    "'this week' -> 7\n"
                    "'positive reviews' -> none\n"
                    "'what are people complaining about' -> none"
                )},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=5,
            timeout=8.0,
            **nothink_kwargs(fast_model),
        )
        answer = resp.choices[0].message.content.strip().lower()
        if answer == "none":
            return None
        days = int(answer)
        return days if days > 0 else None
    except Exception as exc:
        logger.warning("reputation._detect_time_range: failed (%s)", exc)
        return None


REVIEW_PAGE_SIZE = 10
REVIEW_PAGE_STATE_TTL = 1800  # 30 min -- long enough to page through, short enough not to linger


def _review_page_key(store_id: int, from_phone: str) -> str:
    return f"reputation:review_page:{store_id}:{from_phone}"


def _save_review_page_state(
    store_id: int, from_phone: str, sentiment: str | None, status: str | None, offset: int,
    days_back: int | None = None,
) -> None:
    from app.core import cache as _cache
    _cache.set(
        _review_page_key(store_id, from_phone),
        {"sentiment": sentiment, "status": status, "offset": offset, "days_back": days_back},
        ttl=REVIEW_PAGE_STATE_TTL,
    )


def _get_review_page_state(store_id: int, from_phone: str) -> dict | None:
    from app.core import cache as _cache
    return _cache.get(_review_page_key(store_id, from_phone))


def _clear_review_page_state(store_id: int, from_phone: str) -> None:
    from app.core import cache as _cache
    _cache.delete(_review_page_key(store_id, from_phone))


def _list_reviews_page(
    store_id: int, store_name: str, from_phone: str,
    sentiment: str | None, status: str | None, offset: int = 0,
    days_back: int | None = None,
) -> str:
    """One page (REVIEW_PAGE_SIZE reviews) of a sentiment/status/time-
    filtered review list, with NEXT-based pagination. Pagination state
    (which filters, how far in) is stored in Redis -- the same connection
    already used for rate limits/cooldowns/the job queue/freshness caches
    -- keyed per store+phone so two staff members paging through
    different filters at once don't collide. Fails open if Redis is
    down: NEXT will just say there's nothing to continue rather than
    erroring."""
    from app.review_sources import db as review_db

    label = _filter_label(sentiment, status, days_back)
    matches, total = review_db.list_reviews(
        store_id, sentiment=sentiment, status=status, offset=offset, limit=REVIEW_PAGE_SIZE,
        days_back=days_back,
    )
    if not matches:
        if offset == 0:
            return f"*{store_name}* - No {label} reviews found."
        _clear_review_page_state(store_id, from_phone)
        return f"*{store_name}* - No more {label} reviews."

    start = offset + 1
    end = offset + len(matches)
    lines = [f"*{store_name}* - {label.capitalize()} reviews ({start}-{end} of {total}):\n"]
    for i, m in enumerate(matches, start):
        rating = m.get("rating")
        platform = "Instagram" if (m.get("source_platform") or "").startswith("Instagram") else "Google Maps"
        # Instagram content (posts/comments) never has a star rating --
        # showing "no rating" on every single line was just noise, not a
        # real gap in the data. Google Maps reviews do have one, so still
        # show it there (including the rare Google Maps row missing one).
        rating_part = f", {rating}/5" if rating else (", no rating" if platform == "Google Maps" else "")
        post_date = m.get("post_date")
        date_str = post_date[:10] if post_date else "date unknown"
        excerpt = _excerpt(m.get("content_text") or "", 200)
        lines.append(f"{i}. [{platform}{rating_part}, {date_str}] {excerpt}")

    next_offset = offset + len(matches)
    if next_offset < total:
        from app.core import cache as _cache
        if _cache.available():
            _save_review_page_state(store_id, from_phone, sentiment, status, next_offset, days_back)
            lines.append(f"\nType *NEXT* for more ({total - next_offset} remaining).")
        # Redis down: no point offering NEXT if there's nowhere to persist
        # which page comes next -- silently omit the hint rather than
        # promise something that won't work.
    else:
        _clear_review_page_state(store_id, from_phone)
    return "\n".join(lines)


def _chat_about_reviews(store_id: int, store_name: str, text: str) -> str:
    from app.review_sources import db as review_db
    from app.core.llm import get_client, get_model, nothink_kwargs

    pending = review_db.get_pending_finding(store_id)

    # Two complementary sources instead of just "10 most recent": a store
    # can accumulate hundreds of reviews (confirmed live: 300+), and a
    # question like "has anyone complained about parking" needs to find
    # that review regardless of how old it is -- recency alone would miss
    # it entirely if it's not in the newest 10. Semantic search (matching
    # against the actual review TEXT, same embedding pattern as the
    # customer agent's KB search) surfaces what's relevant to THIS
    # question; a small recent sample stays alongside it so general
    # "how are we doing overall"-type questions -- where similarity to
    # the question itself isn't meaningful -- still have something to go on.
    sentiment_lean = _detect_sentiment_lean(text)
    relevant_reviews = review_db.search_reviews_semantic(store_id, text, top_k=15, sentiment=sentiment_lean)
    recent_reviews = review_db.get_recent_reviews(store_id, limit=5)
    seen_ids = {r["id"] for r in relevant_reviews}
    recent_reviews = [r for r in recent_reviews if r["id"] not in seen_ids]

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

    def _format_review_list(reviews: list[dict]) -> str:
        lines = []
        for i, r in enumerate(reviews, 1):
            lines.append(
                f"{i}. [{r.get('source')}] {r.get('rating') or 'N/A'}/5: "
                f"\"{(r.get('text') or '')[:150]}\""
            )
        return "\n".join(lines)

    recent_ctx = "RECENT REVIEWS (general context):\n"
    if relevant_reviews or recent_reviews:
        if relevant_reviews:
            recent_ctx = (
                "REVIEWS MOST RELEVANT TO THE QUESTION BELOW (search the "
                "actual review text, not just recency):\n"
                + _format_review_list(relevant_reviews) + "\n\n"
            )
        if recent_reviews:
            recent_ctx += "ALSO RECENT (general context):\n" + _format_review_list(recent_reviews) + "\n"
    else:
        recent_ctx += "(None, type CHECK to scrape new reviews)\n"

    system = (
        f"You assist the owner of '{store_name}' with review management on WhatsApp. "
        "Be brief, direct and conversational. If the reviews below don't "
        "cover what's being asked, say so plainly instead of guessing.\n"
        "Commands: *POST* (mark draft as replied -- owner still has to post it "
        "on the actual platform themselves, we can't publish it for them), "
        "*EDIT <text>* (revise draft), "
        "*IGNORE* (skip), *CHECK* (scrape new reviews), "
        "*POSITIVE REVIEWS* / *NEGATIVE REVIEWS* / *ALL REVIEWS* (list reviews "
        "10 at a time), *NEXT* (see the next 10).\n\n"
        "WhatsApp format: no markdown, no em-dashes (use a comma or colon instead), "
        "no emojis, use *word* for bold, short paragraphs.\n\n"
        f"{pending_ctx}\n{recent_ctx}"
    )

    try:
        client = get_client()
        resp = client.chat.completions.create(
            # Confirmed live (docs/agent_test_results.md): this exact call
            # took 20-45s even before semantic search was added -- 20s was
            # already marginal. _detect_sentiment_lean above adds its own
            # latency before this call even starts, on top of that
            # existing variance. Matches revenue/scout's more generous
            # final-answer timeouts (25s/90s) rather than a classification
            # call's tight one.
            timeout=45.0,
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
            "*CHECK* - scrape new reviews\n"
            "*POSITIVE REVIEWS* / *NEGATIVE REVIEWS* / *ALL REVIEWS* - list reviews, 10 at a time\n"
            "*NEXT* - see the next 10"
        )
