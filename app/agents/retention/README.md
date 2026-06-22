# 🔁 Retention Agent — Lapsing Regulars & Win-Back Value

**Question it answers:** *Which loyal customers are slipping away, and how much
revenue could we recover by bringing them back?*

## Files in this folder

| File          | What it is                                               |
|---------------|----------------------------------------------------------|
| `analyzer.py` | The logic. Entry point: `analyze_retention()`.           |
| `cli.py`      | Command-line runner that prints a full report.           |
| `README.md`   | You are here.                                            |

## Run it

```bash
python -m app.agents.retention.cli
```

## How "lapsing" is decided

We don't use a one-size-fits-all rule. Each customer has their **own normal
cadence** (median days between visits). A customer is **lapsing** when the time
since their last visit is well beyond *their* normal rhythm:

```
threshold = max(LAPSE_FLOOR_DAYS, LAPSE_MULTIPLIER × their_median_cadence)
```

So a daily regular who's been gone 9 days is lapsing, while a monthly visitor
who's been gone 9 days is fine.

### Two tiers
- **Tier A — Lapsed regulars:** customers whose cadence is tight (in the
  faster half of all customers) *and* who are now lapsing. Highest-value
  win-back targets.
- **Tier B — All lapsing customers:** anyone breaching their personal cadence.

> ⚠️ Retention is measured only over **identifiable** customers (those with a
> `customer_ref` — i.e. card/loyalty). Anonymous cash buyers are excluded, and
> the report states this coverage caveat.

## Key outputs (`RetentionReport`)

- `repeat_rate`, `unique_customers`, `regular_count`
- `lapsed_regular_winback` / `lapsing_winback` — recoverable PKR per tier
- `customers` — per-customer profiles with cadence, gap, and win-back value

## Tunables (top of `analyzer.py`)

| Constant                 | Meaning                                            |
|--------------------------|----------------------------------------------------|
| `LAPSE_MULTIPLIER`       | How many cadences-late before "lapsing" (def 2.5)  |
| `LAPSE_FLOOR_DAYS`       | Minimum gap before anyone counts as lapsing (10)   |
| `MIN_VISITS_FOR_CADENCE` | Visits needed before we trust a cadence (3)        |

## Tests

`tests/test_retention_ops.py`.
