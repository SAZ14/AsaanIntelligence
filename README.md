# AsaanPay Integrity Agent

A POS-integrated **restaurant integrity agent**. It connects to a venue's
point-of-sale, reconciles every payment, calculates true profit, and detects
revenue **leakage** (theft, excess comps/discounts) and **discrepancies**
(payment mismatches, wrong tax rates). The restaurant owner talks to it over
**WhatsApp** — asking about profit, revenue, leakage, or any staff member — and
gets a branded **PDF audit report** on their phone.

The numbers are computed by exact, deterministic engines; an LLM layer (Claude)
only explains them and answers free-form questions, so the financials are never
guessed.

---

## What it does

| Capability | Where |
|---|---|
| Connect to different POS systems (per venue) | `app/pos/` |
| Reconcile payments, profit/COGS, tax integrity | `app/analysis/reconciliation.py` |
| Detect behavioural leakage (theft voids, comps, discounts) | `app/analysis/integrity.py` |
| Rank findings + LLM summary / Q&A | `app/agents/integrity_agent.py` |
| Owner WhatsApp chat (commands + questions) | `app/whatsapp/` |
| Branded PDF report (Asaan Intelligence style) | `app/report/pdf.py` |
| HTML report | `app/report/render.py` |

---

## Quick start

```bash
pip install fastapi uvicorn pydantic anthropic twilio httpx pytest

# Run the audit on the bundled sample venue (no keys needed):
python scripts/run_integrity_agent.py roastery --no-llm

# Generate the phone-friendly PDF (writes output/audit_roastery.pdf):
python scripts/generate_pdf_report.py roastery

# Run the tests:
pytest -q
```

The repo ships a synthetic café dataset (`data/`) with planted issues, so every
feature works out of the box before any real POS is connected.

---

## Connecting a real venue

Everything venue-specific lives in **`app/venues.py`**.

### 1. Point it at the venue's POS

```python
RESTAURANTS = {
    "myvenue": RestaurantConfig(
        venue_name="My Venue",
        pos_type="csv",                      # or "rest"
        connection={"base_dir": "/path/to/exports"},
        mapping="cafe_generic",              # field map; copy & tweak per POS
    ),
}
```

- **`csv`** — reads `sales_detail.csv`, `menu.csv`, `staff.csv` exports.
- **`rest`** — a cloud POS (Square/Foodics/Toast/…): give `base_url` + `api_key`.
  A flat JSON sales-detail feed works with just a field `mapping`; a differently
  shaped API is a small subclass of `RestPOSConnector`.

Different column/field names? Copy `app/ingest/mappings/cafe_generic.py`, rename
the fields, and set `mapping` to your new module.

### 2. Register the owner's WhatsApp number

```python
OWNER_WHATSAPP = {
    "whatsapp:+923001234567": "myvenue",
}
```

### 3. Set credentials (environment variables)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM summary & free-form Q&A (commands work without it) |
| `TWILIO_ACCOUNT_SID` | WhatsApp (inbound signature check + outbound) |
| `TWILIO_AUTH_TOKEN` | "" |
| `TWILIO_WHATSAPP_FROM` | e.g. `whatsapp:+14155238886` |

### 4. Deploy the WhatsApp webhook

```bash
uvicorn app.whatsapp.webhook:app --host 0.0.0.0 --port 8000
```

Point your Twilio WhatsApp number's **"When a message comes in"** webhook at
`https://<your-host>/whatsapp`. The same host serves the PDF at
`/report/<venue>.pdf`, which Twilio fetches when the owner asks for `report`.

### 5. (Optional) Scheduled digests

The owner gets pushed a **daily report** and a **summarised weekly report** — no
need to ask. Both are deterministic (no LLM cost) and compare against the
previous period so the owner sees direction, not just a number.

```bash
# Daily report — yesterday vs the day before, 8am every day:
0 8 * * *  cd /path/to/repo && python scripts/digest_worker.py --kind daily

# Weekly summary — last 7 days vs the prior week, 8am every Monday:
0 8 * * 1  cd /path/to/repo && python scripts/digest_worker.py --kind weekly
```

Preview without sending (and without Twilio creds): add `--dry-run`. No cron?
Use the built-in loop instead, e.g. `--kind daily --every-min 1440`.

---

## What the owner can text

| Message | Reply |
|---|---|
| `summary` | headline audit + top issue |
| `revenue` | sales total + payment-method mix |
| `profit` | net sales, COGS, gross profit & margin |
| `leakage` | suspected loss, broken down, worst offender |
| `findings` | impact-ranked issues to act on |
| `staff` | whole team ranked by integrity score |
| `staff Bilal` | one person's full breakdown + flagged events |
| `daily` | yesterday's report (sales, profit, leakage vs the day before) |
| `weekly` | the week summarised (totals, trend, top issues vs last week) |
| `report` | the PDF audit, delivered to their phone |
| `refresh` | re-pull the latest POS data |
| *anything else* | grounded answer from the LLM (e.g. "why is profit down?") |

---

## How leakage & discrepancies are detected

- **Theft voids** — items voided *after* being fired to the kitchen, on *cash*
  orders: the serve → collect cash → void signature.
- **Excess comps / discounts** — staff whose comp/discount rates exceed the
  venue baseline (with z-scores vs peers).
- **Payment reconciliation** — every order's collected amount vs what the till
  says was owed (net sales + tax); mismatches are flagged.
- **Tax integrity** — the cash-vs-digital tax lever (15% cash / 5% digital);
  orders taxed at the wrong rate are flagged.
- **Profit** — real gross profit from menu cost (COGS), plus *wasted COGS*
  (food made for comped or fired-then-voided items).

Each finding carries a monetary impact and severity; the agent ranks them and
(with an API key) attaches a recommended action.

---

## Project layout

```
app/
  pos/           POS connectors (interface, csv, rest) + registry
  ingest/        CSV loader + row normalisers + field mappings
  analysis/      integrity, reconciliation, retention engines
  agents/        integrity_agent (deterministic engines + LLM layer)
  report/        pdf (Asaan Intelligence style) + html renderers
  whatsapp/       service (brain), webhook (Twilio), twilio_client (outbound)
  venues.py      per-venue POS config + owner numbers   <-- edit to onboard
  models/        canonical Pydantic models
scripts/         run_integrity_agent, generate_pdf_report, generate_report,
                 digest_worker, whatsapp_alert
tests/           full suite (pytest)
data/            synthetic sample venue (with an answer key)
```

---

## Testing

```bash
pytest -q          # full suite
```

The suite runs fully offline — no API key, no network, no live POS — using the
bundled dataset and fake clients for the LLM and Twilio.
