"""Reusable Anatummy Instagram intelligence flow.

This is the shared core behind both the CLI test script and the WhatsApp
webhook: scrape the target profile via Apify, normalize the posts, and build a
WhatsApp-ready reputation report (Claude when ANTHROPIC_API_KEY is set, with a
deterministic fallback otherwise).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TARGET_URL = "https://www.instagram.com/anatummyisb/"


def _username_from_url(url: str) -> str:
    """Extract the handle from an Instagram profile URL."""
    return url.rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()


TARGET_USERNAME = _username_from_url(TARGET_URL)
ACTOR_ID = "apify/instagram-scraper"

# Pin the scrape to the target handle's own grid; directUrls kept as a hint.
ACTOR_INPUT = {
    "resultsType": "posts",
    "username": [TARGET_USERNAME],
    "directUrls": [TARGET_URL],
    "resultsLimit": 10,
}

WHATSAPP_CHAR_LIMIT = 1500
REPORT_MODEL = "claude-sonnet-4-6"


@dataclass
class NormalizedPost:
    url: str
    caption: str
    timestamp: str
    likes_count: int
    comments_count: int
    owner_username: str
    raw_json: dict[str, Any]


def scrape_target_posts(apify_token: str | None = None) -> list[NormalizedPost]:
    """Scrape and normalize the target profile's recent posts.

    Returns only posts authored by the target handle when any are found
    (the Actor also returns tagged/mention posts from other accounts);
    otherwise returns whatever was scraped.
    """
    token = apify_token or os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set")

    from apify_client import ApifyClient

    client = ApifyClient(token)
    logger.info("Running Actor %s for %s", ACTOR_ID, TARGET_URL)
    run = client.actor(ACTOR_ID).call(run_input=ACTOR_INPUT)
    if run is None:
        raise RuntimeError("Actor call returned None (no run object)")

    # apify-client >=3 returns a typed Run model (snake_case); older returns a dict.
    if isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId")
        status = run.get("status")
    else:
        dataset_id = getattr(run, "default_dataset_id", None)
        status = getattr(run, "status", None)

    if status != "SUCCEEDED":
        raise RuntimeError(f"Actor run status was {status!r}, not SUCCEEDED")
    if not dataset_id:
        raise RuntimeError("Run has no defaultDatasetId")

    raw_items = list(client.dataset(dataset_id).iterate_items())
    posts = [
        NormalizedPost(
            url=it.get("url") or it.get("inputUrl") or "",
            caption=(it.get("caption") or "").strip(),
            timestamp=str(it.get("timestamp") or ""),
            likes_count=int(it.get("likesCount") or 0),
            comments_count=int(it.get("commentsCount") or 0),
            owner_username=it.get("ownerUsername") or it.get("ownerFullName") or "",
            raw_json=it,
        )
        for it in raw_items
    ]

    own = [p for p in posts if p.owner_username.lower() == TARGET_USERNAME]
    logger.info(
        "Scraped %d posts; %d authored by @%s", len(posts), len(own), TARGET_USERNAME
    )
    return own or posts


def _posts_digest(posts: list[NormalizedPost]) -> str:
    lines = []
    for i, p in enumerate(sorted(posts, key=lambda x: x.likes_count, reverse=True), 1):
        cap = p.caption.replace("\n", " ")
        if len(cap) > 240:
            cap = cap[:240] + "..."
        lines.append(
            f"{i}. [{p.timestamp[:10]}] likes={p.likes_count} comments={p.comments_count}\n"
            f"   {cap or '(no caption)'}\n   {p.url}"
        )
    return "\n".join(lines)


def build_report_llm(posts: list[NormalizedPost], api_key: str) -> str:
    """Generate the WhatsApp report with Claude."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    username = posts[0].owner_username if posts else "the restaurant"
    digest = _posts_digest(posts)

    prompt = f"""You are a reputation & social-media analyst for restaurants.
Analyze these {len(posts)} recent Instagram posts from @{username} and write a
report addressed to the restaurant OWNER, formatted for WhatsApp.

POSTS (sorted by likes):
{digest}

Write the report with these sections, using short lines and a few emojis as
section markers (no markdown headers, WhatsApp doesn't render them):
- A 1-2 sentence overall summary
- What content is working
- What customers seem interested in
- Reputation risks or missing opportunities
- 3 clear recommended actions (numbered)
- 1 suggested Instagram caption or reply idea

Hard constraints:
- The ENTIRE message must be UNDER {WHATSAPP_CHAR_LIMIT - 100} characters.
- Be concrete and reference real signals from the posts (engagement, themes).
- Plain text suitable for WhatsApp. No markdown headings."""

    resp = client.messages.create(
        model=REPORT_MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def build_report_fallback(posts: list[NormalizedPost]) -> str:
    """Deterministic report when no LLM key is available (clearly labelled)."""
    if not posts:
        return "No posts were scraped, so no report could be generated."

    username = posts[0].owner_username or "your account"
    total_likes = sum(p.likes_count for p in posts)
    total_comments = sum(p.comments_count for p in posts)
    avg_likes = total_likes / len(posts)
    top = max(posts, key=lambda p: p.likes_count)
    top_cap = top.caption.replace("\n", " ")[:80] or "(no caption)"

    lines = [
        f"Asaan Intelligence — @{username}",
        "(auto-generated; ANTHROPIC_API_KEY not set, so this is a heuristic report)",
        "",
        f"Summary: {len(posts)} recent posts, {total_likes} likes & "
        f"{total_comments} comments total (~{avg_likes:.0f} likes/post).",
        "",
        "Working: Your top post "
        f"({top.likes_count} likes) — \"{top_cap}\". Food/burger content drives engagement.",
        "",
        "Customers interested in: menu items, prices, new-branch news (high comment counts).",
        "",
        "Risks/gaps: Engagement varies post-to-post; few posts may carry replies. "
        "Watch comment response time and consistency of posting.",
        "",
        "Recommended actions:",
        "1. Reply to every comment within 24h to protect reputation.",
        "2. Post more of your top-performing format (signature burgers, pricing).",
        "3. Add a clear call-to-action (location/booking) to each caption.",
        "",
        "Caption idea: \"Hungry in Blue Area? Our signature burgers are calling 🍔 "
        "Tag who you're bringing 👇\"",
    ]
    return "\n".join(lines)


def build_report(
    posts: list[NormalizedPost], anthropic_key: str | None = None
) -> str:
    """Build the WhatsApp report from posts, enforcing the character limit."""
    key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY")

    if not posts:
        report = "No posts scraped — nothing to analyze."
    elif key:
        try:
            report = build_report_llm(posts, key)
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM report failed (%s); using fallback", e)
            report = build_report_fallback(posts)
    else:
        report = build_report_fallback(posts)

    # Warn if the scraped posts aren't authored by the target handle.
    if posts and not all(p.owner_username.lower() == TARGET_USERNAME for p in posts):
        seen = sorted({p.owner_username for p in posts if p.owner_username})
        report = (
            f"⚠️ Heads up: these posts are from {seen or 'unknown accounts'}, "
            f"not @{TARGET_USERNAME}. Verify the handle.\n\n" + report
        )

    if len(report) > WHATSAPP_CHAR_LIMIT:
        report = report[: WHATSAPP_CHAR_LIMIT - 3].rstrip() + "..."
    return report


def generate_report(
    apify_token: str | None = None, anthropic_key: str | None = None
) -> str:
    """Scrape the target profile and return a WhatsApp-ready report string."""
    posts = scrape_target_posts(apify_token)
    return build_report(posts, anthropic_key)
