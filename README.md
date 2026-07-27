# AsaanIntelligence — Central Agent Server

A multi-restaurant WhatsApp intelligence platform. Each restaurant gets its own WhatsApp number. Owners and staff message it to get live competitor intel, review analysis, POS audits, and revenue advice. Customers use the same number for loyalty stamps and deals.

Built with FastAPI, SQLAlchemy, Twilio, Z.AI (GLM-4.7), and Apify.

---

## Agents

| Agent | Trigger keywords | What it does |
|---|---|---|
| **Scout** | `scout`, `competitors`, `intel` | Scrapes competitor Instagram posts, Google reviews, and web — generates a ranked intel report |
| **Reputation** | `check`, `review`, `feedback`, `rating`, `post`, `edit`, `ignore` | Scrapes your own Google Maps / FoodPanda / Instagram reviews, classifies sentiment, drafts replies for owner approval |
| **Integrity** | `audit`, `summary`, `leakage`, `profit`, `staff`, `daily`, `weekly` | Audits POS data for voids, discounts, and anomalies |
| **Revenue** | `revenue`, `sales`, `pricing`, `strategy`, `upsell` | Revenue trend analysis and pricing advice |
| **Customer** | *(non-staff senders)* | Loyalty stamps, leaderboard, deals, winback messages |
| **Loans** *(demo)* | `/loans/*` API + CLI | Scans the customer book, scores relationships, detects cash stress, and decides proactive instant-loan offers vs monitor vs decline-with-alternatives — deterministic policy gate + local LLM narrator ([docs](docs/loan-agent.md)) |

---

## Architecture

```
WhatsApp (customer/owner)
        │
        ▼
Twilio ──► POST /whatsapp
        │
        ▼
  Gateway (app/gateway/main.py)
    │  Looks up store_id from Twilio `To` field
    │  Checks if sender is a store_member
    │
    ├── Staff ──► mode menu (1=internal tools, 2=customer app)
    │               └── internal.py routes to agent by keyword
    │
    └── Customer ──► customer agent (loyalty/community)

Agents call:
  - Apify actors  (scraping)
  - Z.AI / GLM-4.7  (LLM classification, reply drafting, analysis)
  - Supabase PostgreSQL  (via SQLAlchemy)
  - Twilio  (sending WhatsApp alerts)
```

### Data isolation guarantee

`store_id` is **always** derived from the Twilio `To` field → `store_twilio_numbers` table lookup. It is never taken from user input or LLM output. Every database query is scoped with `WHERE store_id = ?` at query level. Staff whitelists (`store_members`) are per-store.

---

## Stack

- **Framework**: FastAPI + Uvicorn
- **Database**: Supabase PostgreSQL via SQLAlchemy ORM
- **LLM**: Z.AI / GLM-4.7 (OpenAI-compatible API)
- **Scraping**: Apify actors (Instagram, Google Maps, Google Search, website crawler)
- **Messaging**: Twilio WhatsApp Business API
- **Scheduling**: APScheduler (winback + leaderboard jobs)
- **Deployment**: Railway

---

## Environment Variables

Only these need to be set (in `.env` locally, Railway Variables in production):

| Variable | Description |
|---|---|
| `DATABASE_URL` | SQLAlchemy connection string to Supabase PostgreSQL |
| `ZAI_API_KEY` | Z.AI API key for GLM-4.7 |
| `ZAI_MODEL` | LLM model name (default: `glm-4.7`) |
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_VALIDATE_SIGNATURE` | `true` in production, `false` in development |
| `APIFY_TOKEN` | Apify API token |

All actor names, scraping limits, and pipeline tuning constants are hardcoded in `app/agents/scout/config.py` — not environment variables.

---

## Local Development

```bash
# Install dependencies
pip install -e ".[test]"

# Copy and fill in env vars
cp .env.example .env

# Run server (starts FastAPI + APScheduler)
python scripts/run_server.py

# Expose locally via ngrok
ngrok http 8000

# Set Twilio sandbox webhook to:
# https://<ngrok-url>/whatsapp
```

---

## Deployment (Railway)

The repo contains `railway.toml` — Railway builds automatically on push to `central-server`.

```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "python scripts/run_server.py"
```

**First deploy:**
1. `railway login && railway link` — link to your Railway project
2. Set env vars in Railway dashboard → Variables tab
3. Push to `central-server` — Railway auto-deploys

**Twilio webhook:** Set to `https://<railway-url>/whatsapp` in Twilio console → Messaging → Sandbox settings (or WhatsApp Senders for production numbers).

---

## Onboarding a New Restaurant

### 1. Register the restaurant in the DB

```bash
# For Anatummy — idempotent, safe to re-run
python scripts/onboard_anatummy.py
```

For new restaurants, create a similar script or use the admin API directly.

### 2. Configure reputation scraping

```
POST /admin/stores/{store_id}/reputation
Content-Type: application/json

{
  "google_maps_terms": ["Restaurant Name", "Restaurant Name City"],
  "google_maps_location": "City, Country",
  "foodpanda_url": "https://www.foodpanda.pk/restaurant/xxx/name",
  "foodpanda_keyword": "Restaurant Name",
  "instagram_usernames": ["instagramhandle"],
  "brand_voice_tone": "Tone description for AI reply drafts",
  "brand_voice_never_say": ["sorry", "unfortunately"]
}
```

### 3. Register the WhatsApp number

Each restaurant needs its own WhatsApp number. Two options:

- **Buy a Twilio number** (~$1/month) and enable it for WhatsApp
- **Bring Your Own Number (BYON)** — register the restaurant's existing Pakistani business number via Twilio's WhatsApp Business API (number stays on its carrier, Meta approval required)

All numbers live under one Twilio account (same `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`).

```
POST /admin/stores/{store_id}/twilio
Content-Type: application/json

{ "whatsapp_number": "whatsapp:+92xxxxxxxxxx" }
```

### 4. Add staff members

```
POST /admin/stores/{store_id}/members
Content-Type: application/json

{ "phone": "whatsapp:+92xxxxxxxxxx", "role": "owner" }
```

### 5. Point Twilio webhook

In Twilio console, set the new number's incoming message webhook to:
```
https://asaanintelligence.up.railway.app/whatsapp
```

Same URL for every restaurant — routing is handled by the `To` field.

---

## WhatsApp Usage (Staff)

Send a message to your restaurant's WhatsApp number from a registered staff number.

```
hi          → mode selection menu
1           → enter staff tools
2           → enter customer app
menu        → return to mode selection
help        → list all commands
```

**Staff tool commands:**

```
scout                    → live competitor intelligence report
check                    → scrape new reviews from all platforms
post                     → publish the pending draft reply
edit <your text>         → revise the pending draft reply
ignore                   → skip the current pending review
review / feedback        → chat about your reviews with AI
audit / summary          → POS integrity audit
revenue / pricing        → revenue and pricing advice
```

---

## Database Schema (key tables)

| Table | Purpose |
|---|---|
| `stores` | One row per restaurant |
| `store_twilio_numbers` | Maps Twilio `To` number → `store_id` |
| `store_members` | Whitelisted staff phones per store |
| `venue_configs` | Customer agent config (loyalty, stamps) |
| `reputation_configs` | Per-store review scraping targets and brand voice |
| `competitors` | Seeded + auto-discovered competitors per store |
| `findings` | Scout intel + review findings (deduplicated by hash) |
| `scout_runs` | Scout run history and freshness tracking |
| `user_sessions` | Tracks staff WhatsApp mode (internal/customer) |

---

## Project Structure

```
app/
  agents/
    scout/          — competitor intelligence
    reputation.py   — review scraping, classification, reply drafting
    integrity/      — POS audit
    revenue/        — revenue analysis
    customer/       — loyalty, stamps, winback
  core/
    db.py           — SQLAlchemy models + init_db()
    llm.py          — Z.AI / GLM-4.7 client
    routing.py      — LLM-based agent classifier
  gateway/
    main.py         — FastAPI app, /whatsapp webhook, admin API
    internal.py     — Staff message router
  review_sources/   — Google Maps, FoodPanda, Instagram scrapers
  whatsapp/         — Twilio send helpers

scripts/
  run_server.py         — Entry point (FastAPI + APScheduler)
  onboard_anatummy.py   — Anatummy onboarding (idempotent)
  reputation_live.py    — Scheduled reputation sweep (all stores)
```
