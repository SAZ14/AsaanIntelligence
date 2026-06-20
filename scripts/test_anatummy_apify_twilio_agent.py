"""End-to-end test: Apify scrape -> reputation agent -> Twilio WhatsApp.

Flow:
  1. Verify required environment variables (secrets are masked when printed).
  2. Scrape 10 recent public Instagram posts via the apify/instagram-scraper Actor.
  3. Normalize each post into a canonical shape.
  4. Run a reputation analysis agent that produces a WhatsApp-ready owner report.
  5. Send the report to the owner's WhatsApp via Twilio (or print only in DRY_RUN).
  6. On Twilio failure, still print the report and a classified diagnosis.

Run:
    python scripts/test_anatummy_apify_twilio_agent.py

This script never prints full secrets. DRY_RUN=1 prints only; DRY_RUN=0 sends.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any

TARGET_URL = "https://www.instagram.com/anatummyisb/"
ACTOR_ID = "apify/instagram-scraper"
ACTOR_INPUT = {
    "resultsType": "posts",
    "directUrls": [TARGET_URL],
    "resultsLimit": 10,
}

WHATSAPP_CHAR_LIMIT = 1500
REPORT_MODEL = "claude-sonnet-4-6"


# ── Secret masking ──

def mask(value: str | None, *, show: int = 3) -> str:
    """Mask a secret for display: keep a few leading chars, hide the rest."""
    if not value:
        return "(unset)"
    n = len(value)
    if n <= show:
        return "*" * n + f" (len {n})"
    return value[:show] + "*" * max(n - show, 3) + f" (len {n})"


def mask_phone(value: str | None) -> str:
    """Mask a phone/sender, keeping country code prefix and last 2 digits."""
    if not value:
        return "(unset)"
    core = value.replace("whatsapp:", "")
    if len(core) <= 5:
        return "whatsapp:" * value.startswith("whatsapp:") + "*" * len(core)
    masked = core[:3] + "*" * (len(core) - 5) + core[-2:]
    return ("whatsapp:" if value.startswith("whatsapp:") else "") + masked


# ── Step 1: environment verification ──

@dataclass
class EnvCheck:
    apify_token: str = ""
    anthropic_key: str = ""
    twilio_sid: str = ""
    twilio_auth: str = ""
    whatsapp_from: str = ""
    whatsapp_to: str = ""
    dry_run: bool = True
    missing: list[str] = field(default_factory=list)
    have_llm: bool = False


def verify_env() -> EnvCheck:
    print("=" * 60)
    print("STEP 1 — Environment verification")
    print("=" * 60)

    apify_token = os.environ.get("APIFY_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_auth = os.environ.get("TWILIO_AUTH_TOKEN", "")

    # Sender: TWILIO_WHATSAPP_NUMBER preferred, else TWILIO_WHATSAPP_FROM.
    wa_number = os.environ.get("TWILIO_WHATSAPP_NUMBER", "")
    wa_from = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    whatsapp_from = wa_number or wa_from

    # Recipient: OWNER_NUMBER preferred, else TWILIO_WHATSAPP_TO.
    owner_number = os.environ.get("OWNER_NUMBER", "")
    wa_to = os.environ.get("TWILIO_WHATSAPP_TO", "")
    whatsapp_to = owner_number or wa_to

    dry_run_raw = os.environ.get("DRY_RUN", "1").strip()
    dry_run = dry_run_raw not in ("0", "false", "False", "no", "")

    print(f"  APIFY_TOKEN              : {mask(apify_token)}")
    print(f"  ANTHROPIC_API_KEY        : {mask(anthropic_key)}")
    print(f"  TWILIO_ACCOUNT_SID       : {mask(twilio_sid)}")
    print(f"  TWILIO_AUTH_TOKEN        : {mask(twilio_auth)}")
    print(f"  TWILIO_WHATSAPP_NUMBER   : {mask_phone(wa_number)}")
    print(f"  TWILIO_WHATSAPP_FROM     : {mask_phone(wa_from)}")
    print(f"  -> sender (From)         : {mask_phone(whatsapp_from)}"
          f"  [{'TWILIO_WHATSAPP_NUMBER' if wa_number else 'TWILIO_WHATSAPP_FROM'}]")
    print(f"  OWNER_NUMBER             : {mask_phone(owner_number)}")
    print(f"  TWILIO_WHATSAPP_TO       : {mask_phone(wa_to)}")
    print(f"  -> recipient (To)        : {mask_phone(whatsapp_to)}"
          f"  [{'OWNER_NUMBER' if owner_number else 'TWILIO_WHATSAPP_TO'}]")
    print(f"  DRY_RUN                  : {dry_run_raw!r}  -> {'PRINT ONLY' if dry_run else 'WILL SEND REAL MESSAGE'}")

    # Determine which required vars are missing (respecting the either/or pairs).
    missing: list[str] = []
    if not apify_token:
        missing.append("APIFY_TOKEN")
    if not anthropic_key:
        missing.append("ANTHROPIC_API_KEY")
    if not twilio_sid:
        missing.append("TWILIO_ACCOUNT_SID")
    if not twilio_auth:
        missing.append("TWILIO_AUTH_TOKEN")
    if not whatsapp_from:
        missing.append("TWILIO_WHATSAPP_NUMBER or TWILIO_WHATSAPP_FROM")
    if not whatsapp_to:
        missing.append("OWNER_NUMBER or TWILIO_WHATSAPP_TO")
    if "DRY_RUN" not in os.environ:
        missing.append("DRY_RUN")

    if missing:
        print("\n  Missing / empty required variables:")
        for m in missing:
            print(f"    - {m}")
    else:
        print("\n  All required variables present.")

    return EnvCheck(
        apify_token=apify_token,
        anthropic_key=anthropic_key,
        twilio_sid=twilio_sid,
        twilio_auth=twilio_auth,
        whatsapp_from=whatsapp_from,
        whatsapp_to=whatsapp_to,
        dry_run=dry_run,
        missing=missing,
        have_llm=bool(anthropic_key),
    )


# ── Steps 2-3: scrape + normalize ──

@dataclass
class NormalizedPost:
    url: str
    caption: str
    timestamp: str
    likes_count: int
    comments_count: int
    owner_username: str
    raw_json: dict[str, Any]


def scrape_posts(apify_token: str) -> list[NormalizedPost]:
    print("\n" + "=" * 60)
    print("STEP 2-3 — Apify scrape + normalize")
    print("=" * 60)

    from apify_client import ApifyClient

    client = ApifyClient(apify_token)
    print(f"  Running Actor '{ACTOR_ID}' for {TARGET_URL} ...")
    run = client.actor(ACTOR_ID).call(run_input=ACTOR_INPUT)

    if run is None:
        raise RuntimeError("Actor call returned None (no run object)")

    # apify-client >=3 returns a typed Run model (snake_case); older returns a dict.
    if isinstance(run, dict):
        run_id = run.get("id")
        dataset_id = run.get("defaultDatasetId")
        status = run.get("status")
    else:
        run_id = getattr(run, "id", None)
        dataset_id = getattr(run, "default_dataset_id", None)
        status = getattr(run, "status", None)

    print(f"  Run ID     : {run_id}")
    print(f"  Dataset ID : {dataset_id}")
    print(f"  Status     : {status}")

    if status != "SUCCEEDED":
        raise RuntimeError(f"Actor run status was {status!r}, not SUCCEEDED")
    if not dataset_id:
        raise RuntimeError("Run has no defaultDatasetId")

    raw_items = list(client.dataset(dataset_id).iterate_items())
    print(f"  Raw items  : {len(raw_items)}")

    posts: list[NormalizedPost] = []
    for it in raw_items:
        posts.append(NormalizedPost(
            url=it.get("url") or it.get("inputUrl") or "",
            caption=(it.get("caption") or "").strip(),
            timestamp=str(it.get("timestamp") or ""),
            likes_count=int(it.get("likesCount") or 0),
            comments_count=int(it.get("commentsCount") or 0),
            owner_username=it.get("ownerUsername") or it.get("ownerFullName") or "",
            raw_json=it,
        ))

    print(f"  Normalized : {len(posts)} posts")
    return posts


# ── Step 4: reputation agent ──

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
    top_cap = (top.caption.replace("\n", " ")[:80] or "(no caption)")

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


def run_agent(posts: list[NormalizedPost], env: EnvCheck) -> str:
    print("\n" + "=" * 60)
    print("STEP 4 — Reputation analysis agent")
    print("=" * 60)

    if not posts:
        report = "No posts scraped — nothing to analyze."
    elif env.have_llm:
        print(f"  Using Claude model: {REPORT_MODEL}")
        try:
            report = build_report_llm(posts, env.anthropic_key)
        except Exception as e:  # noqa: BLE001
            print(f"  LLM report failed ({type(e).__name__}: {e}); using fallback.")
            report = build_report_fallback(posts)
    else:
        print("  ANTHROPIC_API_KEY not set -> using deterministic fallback report.")
        report = build_report_fallback(posts)

    # Enforce the WhatsApp character limit.
    if len(report) > WHATSAPP_CHAR_LIMIT:
        report = report[: WHATSAPP_CHAR_LIMIT - 3].rstrip() + "..."

    print(f"\n  Report length: {len(report)} chars (limit {WHATSAPP_CHAR_LIMIT})")
    print("\n--- REPORT ---\n")
    print(report)
    print("\n--------------")
    return report


# ── Step 5-6: Twilio send ──

def _wa(addr: str) -> str:
    """Ensure a WhatsApp address has the whatsapp: prefix."""
    addr = addr.strip()
    return addr if addr.startswith("whatsapp:") else f"whatsapp:{addr}"


def classify_twilio_error(code: int | None, status: int | None, msg: str) -> str:
    m = (msg or "").lower()
    if status == 401 or code == 20003:
        return "auth — Twilio Account SID / Auth Token rejected"
    if code in (63007,) or "channel" in m or "not a valid whatsapp" in m:
        return "wrong sender — the 'From' is not a configured WhatsApp sender for this account"
    if code in (63016,) or "outside" in m or "freeform" in m or "24" in m or "session" in m:
        return "24-hour WhatsApp window — recipient hasn't messaged you in 24h; use an approved template"
    if code in (63015,) or "sandbox" in m or "not been enabled" in m or "join" in m:
        return "sandbox enrollment — recipient must join the Twilio WhatsApp sandbox first"
    if code in (21211, 21614, 21408):
        return "wrong recipient — the 'To' number is invalid or not WhatsApp-enabled"
    if status in (429,) or code == 63018:
        return "rate limit — too many messages, slow down"
    if status is None and ("connection" in m or "timed out" in m or "network" in m or "resolve" in m):
        return "network issue — could not reach Twilio API"
    return "unclassified — see code/message above"


def send_whatsapp(report: str, env: EnvCheck) -> None:
    print("\n" + "=" * 60)
    print("STEP 5-6 — Twilio WhatsApp delivery")
    print("=" * 60)

    from_addr = _wa(env.whatsapp_from)
    to_addr = _wa(env.whatsapp_to)
    print(f"  From : {mask_phone(from_addr)}")
    print(f"  To   : {mask_phone(to_addr)}")

    if env.dry_run:
        print("  DRY_RUN active -> NOT sending. (Set DRY_RUN=0 to send for real.)")
        return

    if not env.twilio_sid or not env.twilio_auth:
        print("  Cannot send: Twilio credentials missing. Report shown above.")
        return

    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        client = Client(env.twilio_sid, env.twilio_auth)
        message = client.messages.create(body=report, from_=from_addr, to=to_addr)
        print(f"  SENT ✅  message SID: {message.sid}  status: {message.status}")
    except Exception as e:  # noqa: BLE001
        # Report is already printed above (step 4); here we diagnose the failure.
        code = getattr(e, "code", None)
        status = getattr(e, "status", None)
        msg = getattr(e, "msg", None) or str(e)
        print("\n  TWILIO SEND FAILED ❌")
        print(f"    error type   : {type(e).__name__}")
        print(f"    Twilio code  : {code}")
        print(f"    message      : {msg}")
        print(f"    HTTP status  : {status}")
        print(f"    diagnosis    : {classify_twilio_error(code, status, msg)}")
        # Surface the full traceback to stderr for debugging (no secrets in it).
        traceback.print_exc()


# ── Main ──

def main() -> None:
    env = verify_env()

    # APIFY_TOKEN is mandatory to do anything useful.
    if not env.apify_token:
        print("\nFATAL: APIFY_TOKEN missing — cannot scrape. Aborting.")
        sys.exit(1)

    try:
        posts = scrape_posts(env.apify_token)
    except Exception as e:  # noqa: BLE001
        print(f"\nFATAL: Apify scrape failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)

    report = run_agent(posts, env)

    # Save the normalized data + report locally for inspection (no secrets).
    out = {
        "target": TARGET_URL,
        "post_count": len(posts),
        "report": report,
        "posts": [asdict(p) for p in posts],
    }
    out_path = os.path.join(os.path.dirname(__file__), "anatummy_last_run.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n  Saved normalized data + report to {out_path}")
    except OSError as e:
        print(f"\n  (could not write {out_path}: {e})")

    send_whatsapp(report, env)
    print("\nDone.")


if __name__ == "__main__":
    main()
