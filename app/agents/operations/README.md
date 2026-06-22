# 📊 Operations Agent — Traffic, Dayparts & Menu Performance

**Question it answers:** *How does this venue actually run — when is it busy,
how do people pay, and which menu items make or lose money?*

## Files in this folder

| File          | What it is                                               |
|---------------|----------------------------------------------------------|
| `analyzer.py` | The logic. Entry point: `analyze_operations()`.          |
| `cli.py`      | Command-line runner that prints a full report.           |
| `README.md`   | You are here.                                            |

## Run it

```bash
python -m app.agents.operations.cli
```

## What it computes

- **Traffic** by hour of day, by day of week (normalized per occurrence), and
  by **daypart** (Morning, Midday, Afternoon, Evening, …). Surfaces the
  busiest and deadest windows.
- **Channel mix** — dine-in vs takeaway.
- **Payment mix** — cash vs card/wallet/qr (the digital share).
- **Menu performance** — every item ranked by volume and by margin, so you can
  see the **heroes** (high margin) and the **dogs** (low margin).

## Key outputs (`OperationsReport`)

- `busiest_hour` / `deadest_hour`, `busiest_day` / `deadest_day`,
  `busiest_daypart` / `deadest_daypart`
- `channels`, `payment_shares`
- `items_by_volume`, `items_by_margin`

## Tunables (top of `analyzer.py`)

`DAYPARTS` defines the named time windows. Change the hour boundaries there to
match how this venue thinks about its day.

## Tests

`tests/test_retention_ops.py`.

---

> 📝 Note for interns: this agent used to live bundled inside `retention.py`.
> It was pulled into its own folder during the repo cleanup because operations
> and retention answer completely different questions and share no code.
