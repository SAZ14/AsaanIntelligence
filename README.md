# AsaanPay Enterprise — Venue Audit Engine

An **offline audit tool** for café / restaurant POS exports. You feed it three
CSVs (sales, menu, staff) — plus reviews — and it produces an owner-facing
report covering money leakage, customer retention, operations, and online
reputation.

No live POS connection, no database. Everything runs locally off CSV files.

---

## 🧭 How the repo is organized (read this first)

The codebase is split into **four agents** plus shared plumbing. Each agent is
a self-contained folder: its analysis logic, its command-line runner, and its
own README live together. **If you own an agent, you can stay inside its
folder.**

```
AsaanPayEnterprise/
│
├── app/
│   │
│   ├── core/                  ← SHARED foundation. Everyone depends on this.
│   │   ├── models.py            Canonical data types (Order, MenuItem, Staff…)
│   │   └── ingest/              CSV loader + per-POS column mappings
│   │
│   ├── agents/                ← THE FOUR AGENTS. One folder each.
│   │   ├── integrity/           💸 Money leakage & staff theft
│   │   ├── retention/           🔁 Lapsing regulars & win-back value
│   │   ├── operations/          📊 Traffic, dayparts, menu performance
│   │   └── reputation/          ⭐ Reviews → shifts, replies (uses Claude)
│   │
│   └── reporting/             ← SHARED: combines agents into one HTML report
│
├── tools/                     ← Dev utilities (data generation, sanity check)
├── data/                      ← The CSV dataset (+ a held-out test set)
├── tests/                     ← Test suite (one file per agent)
└── pyproject.toml
```

### Who owns what

| Agent folder              | What it answers                                      | Needs Claude API? |
|---------------------------|------------------------------------------------------|:-----------------:|
| `app/agents/integrity`    | "Is a staff member stealing? How much is leaking?"   | No                |
| `app/agents/retention`    | "Which regulars are slipping away, and what's it worth to win them back?" | No |
| `app/agents/operations`   | "When are we busy? Which menu items make/lose money?"| No                |
| `app/agents/reputation`   | "What are reviewers complaining about, and what shift caused it?" | **Yes** |

Each of those folders has its own `README.md` explaining the method in detail.

---

## 🚀 Quick start

```bash
# 1. Install dependencies (Python 3.11+)
pip install -e .

# 2. Sanity-check that the data loads
python tools/sanity_check.py

# 3. Run any single agent
python -m app.agents.integrity.cli      # leakage report
python -m app.agents.retention.cli      # win-back report
python -m app.agents.operations.cli     # operations report
python -m app.agents.reputation.cli     # reviews (needs ANTHROPIC_API_KEY)

# 4. Build the combined owner-facing HTML report
python -m app.reporting.cli             # → output/audit_report.html
```

> The reputation agent calls the Claude API, so set `ANTHROPIC_API_KEY` in your
> environment before running it.

---

## 🏗️ The data flow

Every agent follows the same shape, so once you understand one you understand
all of them:

```
   CSV files ──► app.core.ingest ──► canonical models ──► an agent ──► a Report
   (data/)        (loader)            (app.core.models)    (analyze_*)   (dataclass)
                                                              │
                          app.reporting ◄────────────────────┘
                          (renders all agents into one HTML page)
```

1. **Ingest** (`app/core/ingest`) reads the CSVs and converts them into clean
   Python objects. POS systems all name their columns differently, so the
   column→field mapping lives in `app/core/ingest/mappings/`. Supporting a new
   POS = adding one mapping file, no other code changes.
2. **Models** (`app/core/models.py`) are the canonical types every agent
   speaks: `Order`, `LineItem`, `Payment`, `MenuItem`, `Staff`, `Review`.
3. **Agents** each expose one `analyze_*()` / `run_*()` function that takes
   those models and returns a typed `*Report` dataclass.
4. **Reporting** (`app/reporting`) takes the agent reports and renders the
   final HTML.

---

## 🧪 Testing

```bash
pytest                       # run everything
pytest tests/test_integrity.py   # one agent
```

Tests live in `tests/`, one file per agent (`test_integrity.py`,
`test_retention_ops.py`, `test_reputation.py`, etc.). `data/holdout/` is a
second dataset with a *different* planted offender — `test_holdout.py` uses it
to prove the detection logic generalizes and isn't overfit to the main data.

---

## 📂 The dataset

`data/` holds a **synthetic but realistic** café dataset with deliberately
planted patterns (a thieving staff member, lapsing regulars, etc.) so you can
confirm the engine actually detects them. The full data dictionary and the
"answer key" of what each agent should find is in
[`data/DATASET_README.md`](data/DATASET_README.md). Regenerate the held-out set
with `python tools/generate_holdout.py`.
