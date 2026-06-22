# 📰 Reporting — The Combined Owner-Facing Report

This is **shared infrastructure, not an agent.** It takes the outputs of the
integrity, retention, and operations agents and renders a single self-contained
HTML page for the venue owner.

## Files in this folder

| File          | What it is                                                       |
|---------------|------------------------------------------------------------------|
| `render.py`   | `compute_headlines()` (the two headline numbers) + `generate_report()` (the HTML). |
| `cli.py`      | Runs every agent and writes `output/audit_report.html`.          |
| `README.md`   | You are here.                                                    |

## Run it

```bash
python -m app.reporting.cli      # writes output/audit_report.html
```

## The two headline numbers

`compute_headlines()` distills the whole audit into the two numbers an owner
cares about:

1. **Monthly leakage** — money lost, attributable to flagged staff (from the
   integrity agent), scaled to a 30-day month.
2. **Potential recoverable / month** — win-back value from lapsed regulars
   (from the retention agent), based on each customer's *observed* spend rate
   while active, at a conservative recovery rate.

It also runs a **sanity check**: if win-back exceeds 10% of monthly revenue the
report shows a warning, because that usually means the lapse criteria need
review before presenting to a client.

## Tunables (top of `render.py`)

| Constant                   | Meaning                                         |
|----------------------------|-------------------------------------------------|
| `DEFAULT_RECOVERY_RATE`    | Assumed % of lapsed value you actually win back |
| `WINNABLE_GAP_MAX_DAYS`    | Don't count customers gone longer than this     |
| `SANITY_WINBACK_PCT_WARN`  | Warn if win-back exceeds this share of revenue  |

## Tests

`tests/test_report.py`.
