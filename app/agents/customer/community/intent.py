"""Embedding-based intent classification for customer chat.

Currently scoped to the menu intent only. Falls back to regex matching
when the embedding model is unavailable (mirrors the _get_client() /
_embedding_model() None-handling pattern used elsewhere in this codebase).

Menu intent is decided by nearest-example cosine similarity against two
example sets (menu vs. not-menu), not a bare threshold on menu examples
alone — short phrases like "what's up" surface-match "what's good here"
closely enough to clear a bare threshold, so the not-menu set acts as a
competing class the query has to beat, not just a floor it has to clear.
"""
from __future__ import annotations

import re

from app.agents.customer.community.store import _embedding_model

# Regex fallback, used only when the embedding model can't be loaded.
_MENU_RE = re.compile(
    r"\b(menu|what.?s new|deals?|specials?|prices?|recommend|latte|coffee|cake|croissant|mocha|items?|food|eat|burger|chicken|beef)\b",
    re.I,
)
_MENU_PHRASE_RE = re.compile(
    r"\bwhat.{0,20}\byou\b.{0,15}\b(have|got|offer|sell|serve)\b|\bwhat.?s\s+available\b",
    re.I,
)

MENU_INTENT_EXAMPLES = [
    "what's on the menu",
    "what do you have",
    "what do you guys have",
    "what do you offer",
    "what's for lunch",
    "what's for dinner",
    "I'm hungry",
    "what food do you have",
    "show me the menu",
    "what can I order",
    "what's available to eat",
    "any recommendations",
    "what's good here",
    "what's new",
    "what do you sell",
    "how much is the latte",
    "what's the price of the burger",
    "how much does the cake cost",
    "what are your prices",
    "in the mood for something sweet",
    "I feel like eating something",
    "any deals or specials today",
]

# Hard negatives: phrases that surface-pattern-collide with MENU_INTENT_EXAMPLES
# (e.g. "what's up" vs "what's good here") or share vocabulary with the
# price-query examples above ("how much do stamps cost" vs "how much is the
# latte") without being menu requests.
NOT_MENU_EXAMPLES = [
    "what's up",
    "how are you",
    "how's it going",
    "what's going on",
    "how you doing",
    "my stamps",
    "how many stamps do I have",
    "how much do stamps cost",
    "leaderboard",
    "show ranking",
    "where are you located",
    "what's your address",
    "what time do you open",
    "when do you close",
    "do you deliver",
    "order online",
    "thanks",
    "thank you",
    "ok",
    "yes",
    "no",
    "can I get a refund",
    "is this the right number",
    "who are you",
    "nothing for now",
]

MENU_INTENT_THRESHOLD = 0.5

_menu_example_embeddings = None
_not_menu_example_embeddings = None


def _menu_examples():
    global _menu_example_embeddings
    if _menu_example_embeddings is None:
        model = _embedding_model()
        if model is None:
            return None
        _menu_example_embeddings = model.encode(MENU_INTENT_EXAMPLES)
    return _menu_example_embeddings


def _not_menu_examples():
    global _not_menu_example_embeddings
    if _not_menu_example_embeddings is None:
        model = _embedding_model()
        if model is None:
            return None
        _not_menu_example_embeddings = model.encode(NOT_MENU_EXAMPLES)
    return _not_menu_example_embeddings


def is_menu_intent(text: str, threshold: float = MENU_INTENT_THRESHOLD) -> bool:
    """True if `text` is a menu request, via embedding similarity.

    A query counts as menu intent only if it clears `threshold` against
    MENU_INTENT_EXAMPLES *and* scores higher there than against
    NOT_MENU_EXAMPLES. Falls back to regex matching when the embedding
    model isn't available.
    """
    model = _embedding_model()
    menu_examples = _menu_examples()
    not_menu_examples = _not_menu_examples()
    if model is None or menu_examples is None or not_menu_examples is None:
        return bool(_MENU_RE.search(text)) or bool(_MENU_PHRASE_RE.search(text))

    from sentence_transformers import util

    query_embedding = model.encode(text)
    menu_sim = float(util.cos_sim(query_embedding, menu_examples)[0].max())
    not_menu_sim = float(util.cos_sim(query_embedding, not_menu_examples)[0].max())
    return menu_sim >= threshold and menu_sim > not_menu_sim
