"""Shared staff-assistant persona/formatting baseline, reused by every
agent's natural-language answer prompt (integrity, revenue, reputation,
scout) and by gateway/internal.py's _adapt_response rewrite pass, so the
four agents read as one consistent assistant instead of four separately
tuned bots stitched together.

Audit finding: each agent had its own ad hoc system prompt with its own
wording for the same rules (WhatsApp formatting, "don't invent data") --
integrity and revenue didn't even use a system-role message at all, just
folded everything into the user prompt. Callers here layer their own
one-line role framing and domain-specific instructions (grounding data,
commands, depth/style guidance) on top of this shared base; this module
owns only the parts that should be identical everywhere: tone/format and
the honesty rule against inventing facts.

Deliberately no name or self-introduction -- neutral, consistent
professional voice rather than a branded persona (the assistant should
feel like one competent person answering, not announce itself)."""
from __future__ import annotations

WHATSAPP_FORMAT_RULES = (
    "WhatsApp formatting: plain text, no markdown headers (no ##), "
    "*single asterisks* for bold (never **double**), no em-dashes "
    "(use a comma or colon instead), no emojis, short paragraphs so "
    "it's easy to read on a phone."
)

GROUNDING_RULE = (
    "Answer using ONLY the data provided below -- never invent numbers, "
    "names, or facts. If the data doesn't contain the answer, say so "
    "plainly instead of guessing."
)


def staff_persona(role_line: str, store_id: int) -> str:
    """role_line: one sentence describing this agent's domain-specific
    role, e.g. "You are a POS-integrity analyst." (no need to name the
    restaurant -- that's handled here, once, for every agent, instead of
    each caller re-fetching/re-formatting it their own way.)

    store_id: fetches this store's name and (if set) description from the
    stores table and prepends them as shared identity context, so every
    agent's answers are grounded in what this specific restaurant actually
    is, not just generic advice that happens to mention its name.

    Returns the shared system-prompt prefix; callers append their own
    grounding data context, domain-specific instructions, and the user's
    question after this."""
    from app.core.db import SessionLocal, Store

    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()

    name = store.name if store else "this restaurant"
    identity = f"You are the internal staff assistant for {name}."
    if store and store.description:
        identity += f" About this restaurant: {store.description}"

    return f"{identity} {role_line} {GROUNDING_RULE}\n\n{WHATSAPP_FORMAT_RULES}"
