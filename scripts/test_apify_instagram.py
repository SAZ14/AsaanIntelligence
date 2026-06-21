"""Smoke test for the Apify Instagram scraper.

Runs the `apify/instagram-scraper` Actor against a target profile and prints a
short summary of the results. On failure it prints the exact error and
classifies it into one of a fixed set of categories so the cause is obvious.

Usage:
    python scripts/test_apify_instagram.py

Requires the APIFY_TOKEN environment variable and the `apify-client` package.
"""

from __future__ import annotations

import os
import sys
import traceback

TARGET_URL = "https://www.instagram.com/anatummyisb/"
ACTOR_ID = "apify/instagram-scraper"

ACTOR_INPUT = {
    "resultsType": "posts",
    "directUrls": [TARGET_URL],
    "resultsLimit": 10,
}

# Failure categories
CAT_MISSING_TOKEN = "missing APIFY_TOKEN"
CAT_AUTH = "Apify auth issue"
CAT_ACTOR_INPUT = "Actor/input issue"
CAT_BILLING = "Apify usage/billing issue"
CAT_INSTAGRAM = "Instagram profile issue"
CAT_CODE = "code issue"


def classify_error(exc: Exception) -> str:
    """Map an exception to one of the known failure categories."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)

    # Use apify-client's typed exceptions when available (most reliable signal).
    try:
        from apify_client.errors import (
            ForbiddenError,
            RateLimitError,
            UnauthorizedError,
        )

        if isinstance(exc, (UnauthorizedError, ForbiddenError)):
            return CAT_AUTH
        if isinstance(exc, RateLimitError):
            return CAT_BILLING
    except ImportError:
        pass

    # Auth / token problems
    if status in (401, 403) or "unauthor" in msg or "token" in msg or "forbidden" in msg:
        return CAT_AUTH

    # Billing / usage limits
    if status in (402, 429) or any(
        term in msg
        for term in ("payment", "billing", "usage", "quota", "limit exceeded",
                     "insufficient", "plan", "credit")
    ):
        return CAT_BILLING

    # Actor not found / bad input
    if status in (400, 404) or any(
        term in msg
        for term in ("actor", "input", "schema", "not found", "invalid")
    ):
        return CAT_ACTOR_INPUT

    # Instagram-side problems surfaced by the Actor run
    if any(
        term in msg
        for term in ("instagram", "private", "profile", "not exist",
                     "no posts", "login", "challenge", "rate limit")
    ):
        return CAT_INSTAGRAM

    return CAT_CODE


def fail(category: str, exc: Exception) -> None:
    """Print the exact error and its classification, then exit non-zero."""
    print("\n=== FAILURE ===", file=sys.stderr)
    print(f"Category : {category}", file=sys.stderr)
    print(f"Error    : {type(exc).__name__}: {exc}", file=sys.stderr)
    print("\n--- Traceback ---", file=sys.stderr)
    traceback.print_exc()
    sys.exit(1)


def main() -> None:
    # Step 1: check APIFY_TOKEN
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        fail(CAT_MISSING_TOKEN, RuntimeError("APIFY_TOKEN environment variable is not set"))

    # Import after the token check so a missing dependency is clearly a code/env issue.
    try:
        from apify_client import ApifyClient
    except ImportError as exc:
        fail(CAT_CODE, RuntimeError(f"apify-client is not installed: {exc}"))

    # Step 3: build client
    try:
        client = ApifyClient(token)
    except Exception as exc:  # noqa: BLE001 - we classify and report
        fail(classify_error(exc), exc)

    # Steps 4-6: run the Actor and wait for it to finish
    print(f"Running Actor '{ACTOR_ID}' for {TARGET_URL} ...")
    try:
        run = client.actor(ACTOR_ID).call(run_input=ACTOR_INPUT)
    except Exception as exc:  # noqa: BLE001
        fail(classify_error(exc), exc)

    if run is None:
        fail(CAT_CODE, RuntimeError("Actor call returned None (no run object)"))

    # apify-client >=3 returns a typed `Run` pydantic model (snake_case attributes),
    # while older versions returned a dict. Support both.
    if isinstance(run, dict):
        run_id = run.get("id")
        dataset_id = run.get("defaultDatasetId")
        status = run.get("status")
    else:
        run_id = getattr(run, "id", None)
        dataset_id = getattr(run, "default_dataset_id", None)
        status = getattr(run, "status", None)
    print(f"Run finished with status: {status}")

    if status != "SUCCEEDED":
        fail(
            classify_error(RuntimeError(f"Actor run did not succeed: status={status}")),
            RuntimeError(f"Actor run status was {status!r}, not SUCCEEDED"),
        )

    if not dataset_id:
        fail(CAT_CODE, RuntimeError("Run has no defaultDatasetId"))

    # Step 7: fetch items
    try:
        items = list(client.dataset(dataset_id).iterate_items())
    except Exception as exc:  # noqa: BLE001
        fail(classify_error(exc), exc)

    # Step 8: print summary
    print("\n=== RESULTS ===")
    print(f"Run ID         : {run_id}")
    print(f"Dataset ID     : {dataset_id}")
    print(f"Result count   : {len(items)}")

    if not items:
        # An empty dataset on a successful run usually means a private/empty profile.
        fail(
            CAT_INSTAGRAM,
            RuntimeError(
                "Run succeeded but returned 0 items "
                "(profile may be private, empty, or unavailable)"
            ),
        )

    print("\nFirst 3 results:")
    for i, item in enumerate(items[:3], start=1):
        print(f"\n[{i}]")
        print(f"  url           : {item.get('url')}")
        caption = item.get("caption")
        if isinstance(caption, str) and len(caption) > 200:
            caption = caption[:200] + "..."
        print(f"  caption       : {caption}")
        print(f"  timestamp     : {item.get('timestamp')}")
        print(f"  likesCount    : {item.get('likesCount')}")
        print(f"  commentsCount : {item.get('commentsCount')}")


if __name__ == "__main__":
    main()
