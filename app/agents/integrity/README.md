# 💸 Integrity Agent — Money Leakage & Staff Theft

**Question it answers:** *Is money quietly leaking out of the till, and which
staff member is responsible?*

## Files in this folder

| File          | What it is                                                |
|---------------|-----------------------------------------------------------|
| `analyzer.py` | The detection logic. Entry point: `analyze_integrity()`.  |
| `cli.py`      | Command-line runner that prints a full report.            |
| `README.md`   | You are here.                                             |

## Run it

```bash
python -m app.agents.integrity.cli
```

## What it detects

Leakage shows up as three kinds of "adjustments" that a dishonest staffer can
abuse. For each staff member we compare their rate against the **venue
baseline** (everyone else) and only count the *excess*:

1. **Theft voids** — a line voided *after it was sent to the kitchen*
   (`void_after_fire`) on a **cash** order. This is the classic
   serve → collect cash → void the ticket → pocket the cash signature. Highest
   confidence flag, weighted most heavily.
2. **Excess comps** — free items handed out well above the staff baseline.
3. **Excess discounts** — manual discounts above the baseline.

Each staffer gets an **integrity score** (100 = clean). The worst offender, the
suspected monthly leakage in PKR, and the top flagged events all come out in
the `IntegrityReport`.

## Key outputs (`IntegrityReport`)

- `worst_offender` — staff id with the lowest integrity score
- `estimated_leakage_monthly` — headline rupee figure
- `staff_integrity` — per-staff breakdown, sorted worst → best
- `flagged_events` — individual suspicious lines, ranked by value

## Tunables (top of `analyzer.py`)

The integrity-score penalty weights live in `analyze_integrity` — theft voids
count `3×`, comps `2×`, discounts `1×`. Adjust if you want to weight the kinds
of leakage differently.

## Tests

`tests/test_integrity.py` (main dataset) and `tests/test_holdout.py` (a second
dataset with a different planted offender, to prove it generalizes).
