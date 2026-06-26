from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anthropic

from app.models.canonical import MenuItem, Order, Review, Staff


# ── Config ──

# Model routing: cheap/fast classification on Haiku, customer-facing drafting on Sonnet.
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
DRAFTER_MODEL = "claude-sonnet-4-6"


@dataclass
class BrandVoice:
    """Per-venue brand voice config used to steer the reply drafter.

    Defaults to the Tayto voice. Swap in another venue's config to re-tone
    every drafted reply without touching the agent.
    """

    name: str = "Tayto"
    tone: str = (
        "Warm, genuine, and a little playful. Thank people by name when you "
        "know it, reference what they ordered when you can, own mistakes "
        "plainly without grovelling, and invite them back without being salesy."
    )
    never_say: list[str] = field(default_factory=lambda: [
        "free coffee", "free meal", "discount", "voucher", "compensation",
        "we value your feedback", "sorry for the inconvenience",
        "we apologise for any inconvenience caused", "dear valued customer",
    ])


# Default brand voice if a caller doesn't supply one.
DEFAULT_BRAND = BrandVoice()

ISSUE_CLASSES = [
    "service_speed", "staff_attitude", "food_quality",
    "price", "ambiance", "praise", "other",
]

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


# ── LLM classification ──

def classify_reviews_batch(
    reviews: list[ReviewAnalysis],
    client: anthropic.Anthropic,
) -> list[ReviewAnalysis]:
    for ra in reviews:
        prompt = f"""Classify this review. Respond with EXACTLY two words separated by a comma: issue_class,sentiment

Issue classes: {', '.join(ISSUE_CLASSES)}
Sentiments: positive, negative, neutral, mixed

Review (rating {ra.rating}/5): "{ra.text}"

Reply format: issue_class,sentiment"""

        try:
            resp = client.messages.create(
                model=CLASSIFIER_MODEL,
                max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            )
            parts = resp.content[0].text.strip().lower().split(",")
            if len(parts) >= 2:
                issue = parts[0].strip()
                sent = parts[1].strip()
                if issue in ISSUE_CLASSES:
                    ra.issue_class = issue
                if sent in ("positive", "negative", "neutral", "mixed"):
                    ra.sentiment = sent
        except Exception:
            pass

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
    client: anthropic.Anthropic,
    brand: BrandVoice = DEFAULT_BRAND,
) -> list[ReviewAnalysis]:
    never_say = "; ".join(brand.never_say) if brand.never_say else "(none)"
    system = (
        f"You are the owner of {brand.name}, personally replying to online "
        f"reviews. Voice: {brand.tone}\n\n"
        f"Hard rules:\n"
        f"- Reply in 2-3 sentences, plain text, no emojis, no hashtags.\n"
        f"- Be specific to this reviewer's actual experience — never generic.\n"
        f"- Never use these words/phrases (they sound corporate or over-promise): "
        f"{never_say}.\n"
        f"- Do not invent facts, refunds, or offers. If you don't know a detail, "
        f"don't claim it.\n"
        f"- Write only the reply text, nothing else."
    )

    for ra in reviews:
        if ra.rating >= 5 and ra.issue_class == "praise":
            intent = "Thank them warmly and, if natural, nudge them to try something else next time."
        elif ra.rating <= 2:
            intent = "Apologise plainly, name the specific issue they hit, and say (honestly) that you're looking into it. Invite them back."
        elif ra.rating <= 3:
            intent = "Appreciate the honest feedback and address the specific concern they raised."
        else:
            intent = "A warm, specific thank you."

        context_lines = []
        if ra.correlation.confidence != "none":
            if ra.correlation.estimated_date:
                context_lines.append(f"Visit was likely {ra.correlation.estimated_date}")
            if ra.correlation.estimated_hour_range:
                context_lines.append(f"around {ra.correlation.estimated_hour_range}")
            if ra.correlation.order_count_in_window > 0:
                context_lines.append(f"{ra.correlation.order_count_in_window} orders in that window (busy period)")
            if ra.correlation.matched_staff_name:
                context_lines.append(f"staff involved: {ra.correlation.matched_staff_name}")
        context_str = "; ".join(context_lines) if context_lines else "No reliable visit details available"

        prompt = (
            f'Review by {ra.reviewer_name} — {ra.rating}/5 on {ra.source}:\n'
            f'"{ra.text}"\n\n'
            f"Classified issue: {ra.issue_class} (sentiment: {ra.sentiment})\n"
            f"What we reconstructed about the visit: {context_str}\n\n"
            f"Goal for this reply: {intent}"
        )

        try:
            resp = client.messages.create(
                model=DRAFTER_MODEL,
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            ra.draft_reply = resp.content[0].text.strip()
        except Exception as e:
            ra.draft_reply = f"[draft generation failed: {e}]"

    return reviews


# ── Main agent ──

def run_reputation_agent(
    reviews: list[Review],
    orders: list[Order],
    staff: dict[str, Staff],
    menu: dict[str, MenuItem],
    brand: BrandVoice = DEFAULT_BRAND,
    client: anthropic.Anthropic | None = None,
) -> ReputationReport:
    if client is None:
        client = anthropic.Anthropic()

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
    draft_replies(analyses, client, brand)

    happy = [
        {"reviewer_name": ra.reviewer_name, "review_id": ra.review_id,
         "rating": ra.rating, "source": ra.source}
        for ra in analyses
        if ra.rating >= 5 and ra.sentiment == "positive"
    ]

    avg_rating = sum(r.rating for r in reviews) / len(reviews) if reviews else 0

    return ReputationReport(
        venue_name=brand.name,
        total_reviews=len(reviews),
        avg_rating=avg_rating,
        reviews=analyses,
        patterns=patterns,
        happy_reviewers=happy,
    )


def process_reputation_owner_reply(from_phone: str, body: str) -> None:
    from app.services.messaging import parse_twilio_whatsapp_phone, send_whatsapp_text
    from app.database import supabase
    from app.review_sources import db as review_db

    phone = parse_twilio_whatsapp_phone(from_phone)


    res = supabase.table("store_members").select("store_id").eq("whatsapp", phone).execute()
    if not res or not res.data:
        import os
        from app.services.messaging import normalize_phone
        env_owners = {
            normalize_phone(p.strip())
            for p in os.environ.get("ASAAN_OWNER_PHONES", "").split(",")
            if p.strip()
        }
        if phone in env_owners:
            # Auto-register in store_members table for the primary store (ID 1)
            supabase.table("store_members").insert({
                "store_id": 1,
                "whatsapp": phone,
                "role": "owner"
            }).execute()
            res = supabase.table("store_members").select("store_id").eq("whatsapp", phone).execute()
        
        if not res or not res.data:
            send_whatsapp_text(phone, "You are not registered as an owner for any store.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return


    store_ids = [row["store_id"] for row in res.data]
    active_store_id = store_ids[0]
    if len(store_ids) > 1:
        session_res = supabase.table("user_sessions").select("store_id").eq("whatsapp", phone).execute()
        if session_res.data and session_res.data[0]["store_id"] in store_ids:
            active_store_id = session_res.data[0]["store_id"]
        else:
            supabase.table("user_sessions").upsert({
                "whatsapp": phone,
                "store_id": active_store_id
            }).execute()

    store_res = supabase.table("stores").select("name").eq("id", active_store_id).maybe_single().execute()
    store_name = "Store"
    if store_res and store_res.data:
        if isinstance(store_res.data, dict):
            store_name = store_res.data.get("name", "Store")
        elif isinstance(store_res.data, list) and store_res.data:
            store_name = store_res.data[0].get("name", "Store")


    text = body.strip()
    text_lower = text.lower()

    if text_lower.startswith("post"):
        finding = review_db.get_pending_finding(active_store_id)
        if not finding:
            send_whatsapp_text(phone, f"[{store_name}] There are no pending review drafts awaiting confirmation.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return
        
        summary = finding["ai_summary"]
        summary["status"] = "posted"
        review_db.update_finding_summary(finding["id"], summary)
        
        draft = summary.get("draft_reply", "")
        send_whatsapp_text(phone, f"[{store_name}] Published draft response to Google Maps:\n\n{draft}", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")

    elif text_lower.startswith("edit"):
        edit_content = text[4:].strip()
        if not edit_content:
            send_whatsapp_text(phone, f"[{store_name}] To edit, please reply with *EDIT* followed by your new message (e.g. *EDIT Thanks for the review!*).", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return
            
        finding = review_db.get_pending_finding(active_store_id)
        if not finding:
            send_whatsapp_text(phone, f"[{store_name}] No pending review draft found to edit.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return
            
        summary = finding["ai_summary"]
        summary["draft_reply"] = edit_content
        review_db.update_finding_summary(finding["id"], summary)
        
        send_whatsapp_text(phone, f"[{store_name}] Draft updated. Reply *POST* to publish or *IGNORE* to skip.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")

    elif text_lower.startswith("ignore"):
        finding = review_db.get_pending_finding(active_store_id)
        if not finding:
            send_whatsapp_text(phone, f"[{store_name}] No pending review draft found to ignore.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return
            
        summary = finding["ai_summary"]
        summary["status"] = "ignored"
        review_db.update_finding_summary(finding["id"], summary)
        
        send_whatsapp_text(phone, f"[{store_name}] Skipped review. No reply will be posted.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")

    elif text_lower.startswith("check") or text_lower.startswith("scrape") or text_lower.startswith("run"):
        from app.whatsapp.config import WhatsAppConfig
        send_whatsapp_text(phone, f"[{store_name}] Checking for new reviews...", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
        
        store_res = supabase.table("stores").select("*").eq("id", active_store_id).maybe_single().execute()
        if not store_res or not store_res.data:
            send_whatsapp_text(phone, f"[{store_name}] Error: Store config not found in database.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            return
            
        from scripts.reputation_live import process_store_reviews
        try:
            wa = WhatsAppConfig.from_env()
            added_count = process_store_reviews(store_res.data, wa)
            if added_count == 0:
                send_whatsapp_text(phone, f"[{store_name}] Check completed. No new reviews found.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            else:
                send_whatsapp_text(phone, f"[{store_name}] Check completed. Processed {added_count} new reviews.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
        except Exception as e:
            send_whatsapp_text(phone, f"[{store_name}] Check failed: {e}", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")

    else:
        # Determine intent using Claude
        try:
            client = anthropic.Anthropic()
            intent_resp = client.messages.create(
                model=CLASSIFIER_MODEL,
                max_tokens=10,
                system=(
                    "You are an intent classifier for a restaurant's reputation management WhatsApp bot.\n"
                    "Categorize the user's message into one of these intents:\n"
                    "- 'scrape': only if the user explicitly wants to trigger a fresh, slow live crawler check/scrape/sync run for new updates on platforms.\n"
                    "- 'chat': if they want to view, list, query, or analyze reviews already in the database (e.g., 'give me the last 10 reviews', 'what is the latest feedback'), ask conversational questions, edit drafts, or get recommendations.\n\n"
                    "Respond with exactly one word: 'scrape' or 'chat'."
                ),
                messages=[{"role": "user", "content": text}],
            )
            intent = intent_resp.content[0].text.strip().lower()
        except Exception:
            intent = "chat"

        if "scrape" in intent:
            from app.whatsapp.config import WhatsAppConfig
            send_whatsapp_text(phone, f"[{store_name}] Checking for new reviews across platforms...", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            
            store_res = supabase.table("stores").select("*").eq("id", active_store_id).maybe_single().execute()
            if not store_res or not store_res.data:
                send_whatsapp_text(phone, f"[{store_name}] Error: Store config not found in database.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
                return
                
            from scripts.reputation_live import process_store_reviews
            try:
                wa = WhatsAppConfig.from_env()
                added_count = process_store_reviews(store_res.data, wa)
                if added_count == 0:
                    send_whatsapp_text(phone, f"[{store_name}] Check completed. No new reviews found.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
                else:
                    send_whatsapp_text(phone, f"[{store_name}] Check completed. Processed {added_count} new reviews.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            except Exception as e:
                send_whatsapp_text(phone, f"[{store_name}] Check failed: {e}", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
        else:
            finding = review_db.get_pending_finding(active_store_id)
            context_str = ""
            if finding:
                sum_data = finding.get("ai_summary") or {}
                corr = sum_data.get("correlation") or {}
                context_str = (
                    f"LATEST PENDING REVIEW:\n"
                    f"- Rating: {finding.get('rating')}/5\n"
                    f"- Review Text: \"{finding.get('content_text')}\"\n"
                    f"- Current Draft Reply: \"{sum_data.get('draft_reply')}\"\n"
                    f"- Correlation: Serviced by {corr.get('matched_staff_name') or 'unknown'} on date {corr.get('estimated_date') or 'unknown'}.\n"
                )
            
            # Fetch recent reviews from the database
            recent_reviews = review_db.get_recent_reviews(limit=10)
            recent_str = "RECENT REVIEWS (LAST 10):\n"
            if recent_reviews:
                for idx, r in enumerate(recent_reviews, 1):
                    recent_str += (
                        f"{idx}. [{r.get('source', 'Unknown')}] Rating: {r.get('rating') or 'N/A'}/5 - \"{r.get('text')}\" "
                        f"(Posted: {r.get('review_date') or 'unknown'})\n"
                    )
            else:
                recent_str += "(No recent reviews found in database)\n"
                
            system_prompt = (
                f"You are a helpful, concise AI assistant for the owner of the store '{store_name}'. "
                f"Reply to the owner in a helpful, warm, and professional tone. Keep your response short (under 4 sentences).\n\n"
                f"Use the following context to answer their questions about reviews, performance, or history:\n"
                f"{context_str}\n"
                f"{recent_str}\n\n"
                f"If they want you to rewrite the draft for the latest pending review, provide a revised draft (remind them they can apply it using 'EDIT <message>')."
            )
            
            try:
                client = anthropic.Anthropic()
                resp = client.messages.create(
                    model=CLASSIFIER_MODEL,
                    max_tokens=300,
                    system=system_prompt,
                    messages=[{"role": "user", "content": text}],
                )
                reply_text = resp.content[0].text.strip()
                send_whatsapp_text(phone, reply_text, from_key="TWILIO_WHATSAPP_MERCHANT_FROM")
            except Exception:
                send_whatsapp_text(phone, f"[{store_name}] Command not recognized. Reply:\n*POST* to publish draft\n*EDIT <new message>* to revise\n*IGNORE* to skip\n*CHECK* to scrape new reviews.", from_key="TWILIO_WHATSAPP_MERCHANT_FROM")



