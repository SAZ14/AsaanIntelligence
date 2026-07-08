"""Shared sentence-embedding model loader.

Originally lived only in app/agents/customer/community/store.py (used for
KB search and menu-intent classification). Reputation's semantic review
search needs the exact same model -- pulled out here so both can share one
cached instance without reputation importing from the customer agent's
module tree, or the two ending up on different model versions.
"""
from __future__ import annotations

_EMBEDDING_MODEL = None


def embedding_model():
    """Return a cached SentenceTransformer model, or None if unavailable.

    Loaded once per process (~27s measured live, see
    app/gateway/main.py's _warm_embeddings which forces this at startup
    so real request traffic never pays that cost) and reused forever.
    Every caller must handle a None return -- e.g. no sentence_transformers
    installed locally, or a broken download -- by falling back to a
    non-semantic path rather than raising.
    """
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is not None:
        return _EMBEDDING_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        _EMBEDDING_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _EMBEDDING_MODEL
    except Exception:
        return None
