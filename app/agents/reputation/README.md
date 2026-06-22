# ⭐ Reputation Agent — Reviews → Shifts, and Auto-Drafted Replies

**Question it answers:** *What are reviewers actually complaining about, which
shift/staff likely caused it, and how should the venue reply?*

> 🔑 **This is the only agent that calls the Claude API.** Set
> `ANTHROPIC_API_KEY` before running it.

## Files in this folder

| File        | What it is                                                  |
|-------------|-------------------------------------------------------------|
| `agent.py`  | The logic. Entry point: `run_reputation_agent()`.           |
| `cli.py`    | Command-line runner that prints a full report.              |
| `README.md` | You are here.                                               |

## Run it

```bash
export ANTHROPIC_API_KEY=sk-...
python -m app.agents.reputation.cli
```

## How it works (three stages)

1. **Correlation (deterministic, no LLM).** From the review text we extract any
   day-of-week, time-of-day, staff name, or menu item mentioned, then match it
   against the order log to estimate **which visit** the review is about — and
   how busy that window was. Produces a `confidence` of none/low/medium/high.
2. **Classification + reply drafting (LLM).** Claude classifies each review
   (issue type + sentiment) and drafts a short, on-brand reply that references
   the specific visit context.
3. **Pattern detection (deterministic).** Clusters correlated complaints to
   surface recurring problems (e.g. "3 reviews flag slow service on Friday
   evenings").

### Which models it uses
- Classification: **Claude Haiku** (cheap, high-volume, two-word answer).
- Reply drafting: **Claude Sonnet** (better prose).

These are set in `agent.py` (`classify_reviews_batch` and `draft_replies`).

## Key outputs (`ReputationReport`)

- `reviews` — per-review analysis (correlation + issue + sentiment + draft reply)
- `patterns` — recurring issue clusters
- `happy_reviewers` — 5-star fans worth asking for more reviews

## Tunables (top of `agent.py`)

`DEFAULT_VENUE_NAME`, `DEFAULT_BRAND_VOICE`, `ISSUE_CLASSES`, and the
keyword/time-pattern tables for correlation.

## Tests

`tests/test_reputation.py` — covers the **deterministic** parts only
(correlation + pattern detection). LLM calls are not unit-tested.
