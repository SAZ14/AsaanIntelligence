# AsaanPay Revenue Agent

A POS-driven **revenue advisor** that cafés' owners chat with over WhatsApp. It
reads a café's sales data, tells the owner what's selling, which prices to nudge,
when they're slow, and how to grow revenue — and it **always closes with advice**,
not just numbers.

One bot serves many cafés. Each owner messages the same "AsaanPay Rev Agent"
contact and gets their **own** advisor over their **own** data. Data is never
shared between cafés.

---

## How it works

```
                        ┌─────────────────────────────────────────────┐
                        │   ONE WhatsApp bot: "AsaanPay Rev Agent"      │
                        │   (a contact in every owner's phone)          │
                        └─────────────────────────────────────────────┘
   Sugar Rush owner ───────────────┐   ▲
   SIP owner ──────────────────────┤   │  reply goes back to whoever asked
   Bean Scene owner ───────────────┘   │
                          Twilio → POST /webhook/whatsapp
                                       │
                          "who is texting?" (sender's phone)
                                       │
                            ┌──────────────────────┐
                            │   TENANT REGISTRY     │   owner phone → café
                            │  +9230012… → sugar_rush│
                            │  +9233311… → sip       │
                            └──────────────────────┘
                                       │ route to THAT café's agent
          ┌─────────────────┬──────────┴─────────┬─────────────────┐
          ▼                 ▼                    ▼
   Sugar Rush AGENT     SIP AGENT          Bean Scene AGENT
   reads ONLY           reads ONLY          reads ONLY
   data/cafes/          data/cafes/sip/     data/cafes/
     sugar_rush/        + its own DB          bean_scene/
   + its own DB                              + its own DB

   ❌ no agent can read another café's folder — enforced at startup
```

### Step by step
1. **Onboard a café** → creates its private folder + maps the owner's phone to it.
2. **POS data lands in that café's folder** (nightly export / sync).
3. **Owner texts the bot** a question.
4. **Registry routes by sender** → the right café's agent.
5. **Claude parses** the question → intent + period (keyword fallback if no key).
6. **Agent computes from that café's data only.**
7. **Reply: answer first, then growth advice**, back to the same owner.
8. **Scheduled digests** push automatically (daily/weekly/monthly) per café.

---

## What the owner can ask

| Ask | Gets |
|---|---|
| "How did we do this week?" | revenue, orders, avg ticket, best seller |
| "Best sellers this month" | top products by units / revenue / margin |
| "What can I raise prices on?" | concrete moves: *"X sells ~N/mo → raise PKR 10 → +PKR Z/mo"* (and lower-this hints) |
| "When are we slow?" | quietest day × time windows |
| "Campaign ideas" | brand-safe ways to fill quiet windows |
| "How do I grow revenue?" | full playbook: average ticket, frequency, dayparts, menu |
| "Give me menu advice" | feature high-margin heroes, fix dogs, add missing categories |
| "How do I get repeat customers?" | loyalty programme + win-back |
| "Send me a weekly digest" | scheduled updates |

Every data answer also ends with **💰 Quick price wins** → **📈 Then bigger plays**.

---

## Per-café layout (fully separate)

| Thing | Example |
|---|---|
| POS data | `data/cafes/<id>/sales_detail.csv`, `menu.csv`, `staff.csv` |
| Config | `<id>_config.json` (campaigns, categories, price knobs) |
| Database | `data/<id>.db` (subscriptions + campaign log) |
| Owner phone(s) | in the tenants registry → `<id>` |

### Data contract (each café's folder)
- **`sales_detail.csv`** — one row per line item (order_id, datetime, item_sku, qty,
  unit_price, line_amount, category, payment fields, customer_ref…).
- **`menu.csv`** — sku, name, category, **cost**, price (cost powers margin advice).
- **`staff.csv`** — staff_id, name, role.

If a café's POS exports different column names, write a small mapping (see
`app/ingest/mappings/`) instead of reformatting their files.

---

## Running it

### Onboard a café (one command)
```bash
python -m scripts.revenue_add_cafe \
  --id sip --name "SIP" \
  --owner +923331112222 \
  --config data/sip_config.json      # optional
```
Creates `data/cafes/sip/` (with a `menu.csv` template), registers the owner's
phone, and refuses anything that would share data with another café.

### Serve the one bot for all cafés
```bash
python -m scripts.revenue_multi_serve         # app.revenue.multi_api:app on :8002
```
Point the single Twilio WhatsApp number's inbound webhook at `POST /webhook/whatsapp`.

### Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/whatsapp` | owner Q&A (routed by sender) |
| POST | `/digests/{cadence}` | cron pushes digests across all cafés |
| GET | `/cafes` | list onboarded cafés |
| GET | `/health` | status |

### Environment
```
REVENUE_TENANTS      tenants registry JSON (default data/revenue_tenants.example.json)
REVENUE_USE_CLAUDE   "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM   the bot's credentials
```
Without Twilio it logs instead of sending; without Claude it uses a keyword parser.

---

## Data isolation (enforced 3×)
1. **Onboarding** forces a private folder per café and refuses the shared sample root.
2. **Registry** refuses to start if two cafés share a data folder or database.
3. **Runtime** — each agent only ever reads its own café's folder; no code pools
   data across cafés.

> Sugar Rush for Sugar Rush, SIP for SIP.

---

## Single-café mode (no multi-tenancy)

To run for just one café, use `app.revenue.api:app` (via `scripts/revenue_serve.py`)
with `REVENUE_DATA_DIR`, `REVENUE_CONFIG`, `REVENUE_DB`.

## Use the engine directly (library)
```python
from app.revenue.agent import RevenueAgent
from app.revenue.datasource import load_pos

orders, menu, staff = load_pos("data/cafes/sip")
agent = RevenueAgent(orders=orders, menu=menu, staff=staff)
print(agent.handle_message("+923331112222", "how do I grow revenue").text)
```
Individual pieces are importable too: `pricing.simple_price_moves`,
`analytics.detect_dead_windows`, `strategy.build_playbook`, `segments.build_segments`.

---

## Code map

| File | Role |
|---|---|
| `tenants.py` | multi-café registry + per-owner routing + folder provisioning |
| `multi_api.py` | the one-bot FastAPI service |
| `api.py` | single-café FastAPI service |
| `agent.py` | the advisor engine (computes numbers, composes replies, always advises) |
| `pricing.py` | simple price moves + pricing-power scoring |
| `strategy.py` | growth playbook: average ticket, frequency, dayparts, menu |
| `analytics.py` | product performance, dead windows, campaigns, digests |
| `segments.py` | POS-derived customer segments |
| `nlu.py` | Claude NLU + deterministic fallback |
| `datasource.py` | load POS + period filtering |
| `store.py` | SQLite (subscriptions + campaign log) |
| `whatsapp.py` | Twilio inbound parsing + outbound client |
| `config.py` / `models.py` | tunables + persisted models |

Tests: `tests/test_revenue.py`. Demo: `python -m scripts.revenue_demo`.
