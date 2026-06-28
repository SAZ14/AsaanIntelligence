# Store Onboarding Guide

This document covers everything needed to bring a new restaurant live on the platform — from Twilio setup through to each agent producing real output.

---

## Prerequisites

Before running the onboarding script you need three things in place:

**1. A Twilio WhatsApp number for the restaurant**

Each restaurant branch gets its own WhatsApp number. If the restaurant already has a Pakistani business number they want to use, register it via Twilio's BYON (Bring Your Own Number) — the number stays on its carrier, Twilio just routes WhatsApp messages through it. If they don't care about the number, buy one from the Twilio console.

All numbers go under the same Twilio account (same `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`). You do not need a separate account per restaurant.

**2. The server `.env` populated**

The Railway environment variables must already be set. The minimum required set:
```
DATABASE_URL         Supabase connection string
TWILIO_ACCOUNT_SID   From Twilio console
TWILIO_AUTH_TOKEN    From Twilio console
APIFY_TOKEN          For scout + reputation scraping
ZAI_API_KEY          For AI report generation
```

**3. The Twilio webhook pointing at the server**

In the Twilio console, for each WhatsApp number, set the incoming message webhook to:
```
https://your-railway-domain.up.railway.app/whatsapp
```
Method: HTTP POST. This applies to every number — they all hit the same endpoint; the server resolves the store from the `To` field.

---

## Step 1 — Fill in the onboarding script

Open `scripts/onboard_store.py` and fill in every section marked `← FILL IN`.

### STORE

```python
STORE = {
    "name":             "Burger Lab F-10",
    "location":         "F-10 Markaz, Islamabad",
    "category":         "burgers",
    "instagram_handle": "burgerlabpk",   # without @ — None if not on Instagram
}
```

`category` is used by the scout agent to contextualise competitor reports. Use a simple food category: `burgers`, `pizza`, `desserts`, `cafe`, `biryani`, etc.

### WHATSAPP\_NUMBER

```python
WHATSAPP_NUMBER = "whatsapp:+923001234567"
```

This is the number the restaurant's customers and staff will WhatsApp. Must already be registered in Twilio before you run the script.

### MEMBERS

```python
MEMBERS = [
    {"phone": "whatsapp:+923001234567", "role": "owner"},
    {"phone": "whatsapp:+923009876543", "role": "manager"},
]
```

Role options: `owner`, `manager`, `staff`. Only people in this list get access to the internal staff tools (scout, integrity, revenue, reputation). Everyone else who messages the store number goes straight to the customer loyalty app.

At least one owner should be added — the reputation agent sends negative review alerts to owners.

### REPUTATION

```python
REPUTATION = {
    "google_maps_terms": [
        "Burger Lab F-10",
        "Burger Lab Islamabad F-10",
    ],
    "google_maps_location": "Islamabad, Pakistan",
    "foodpanda_url":     "https://www.foodpanda.pk/restaurant/xxxx/burger-lab",
    "foodpanda_keyword": "Burger Lab",
    "instagram_usernames": ["burgerlabpk"],
    "brand_voice_tone": (
        "Direct and confident. Acknowledge issues without being apologetic. "
        "Invite the reviewer back with a specific offer when relevant."
    ),
    "brand_voice_never_say": ["unfortunately", "we apologize", "we regret"],
}
```

`google_maps_terms` can include multiple variants — the scraper searches each one. Add the branch name, the city name, and any common misspellings if needed.

Set `foodpanda_url` and `foodpanda_keyword` to `None` if the restaurant is not listed on FoodPanda.

`instagram_usernames` are the restaurant's own handles, not competitors. The reputation agent scrapes comments on the restaurant's own posts to catch complaints posted there.

### COMPETITORS

```python
LOCAL = [
    {
        "name": "Smash Bros Burgers",
        "category": "burgers",
        "instagram_handle": "smashbrosburgers",
        "notes": "F-7; known for loaded fries and smash-style patties",
    },
]

NATIONAL = [
    {
        "name": "Crust Bros",
        "category": "burgers",
        "instagram_handle": "crustbros",
        "notes": "Lahore-based; large following; benchmark for content quality",
    },
]

TRENDS = [
    {
        "name": "Global Trend: Smash Burgers",
        "category": "international_trend",
        "instagram_handle": "#smashburger",
        "notes": "Global smash content — viral formats and techniques",
    },
]
```

Instagram handle is optional — if set to `None`, the discovery engine will search for it on the first scout run. For trend hashtags, the handle must start with `#`.

### CUSTOMER\_CONFIG

```python
CUSTOMER_CONFIG = {
    "venue_name":      "Burger Lab F-10",
    "stamp_goal":      10,
    "reward_text":     "1 Free Classic Smash Burger",
    "winback_days":    5,
    "code_expiry_days": 30,
    "owner_phones":    ["whatsapp:+923001234567"],
    "qr_greeting":     "Scan to join our loyalty programme and earn free burgers!",
}
```

`stamp_goal` — how many stamps a customer needs to earn the reward.
`reward_text` — what the reward is, shown in the redemption message.
`winback_days` — if a customer hasn't visited in this many days, they get a winback message.
`owner_phones` — who receives the weekly leaderboard and winback reports.
`qr_greeting` — shown when a customer scans the QR code for the first time (optional).

Leave `CUSTOMER_CONFIG = None` to skip for now; configure later via `POST /admin/stores/{id}/customer`.

---

## Step 2 — Run the script

```bash
python scripts/onboard_store.py
```

The script is fully idempotent — safe to re-run if you correct a value. Output uses:
- `[+]` — created
- `[~]` — updated (already existed, fields changed)
- `[=]` — already correct, nothing changed
- `[!]` — skipped, action needed

At the end the script prints the store's ID and any remaining steps.

---

## Step 3 — Activate each agent

After the script runs, each agent needs one more step before it produces live output.

### Scout agent

**Status after script:** Ready immediately.

The script seeded your competitor list. On the first `scout` command from a staff member, the agent runs a live scrape of all competitors across Instagram, Google Maps reviews, and their websites.

**To trigger:**
Staff member sends `scout` in WhatsApp (internal mode). The first run takes 2–5 minutes. Subsequent requests within 90 minutes serve the cached report.

---

### Reputation agent

**Status after script:** Ready immediately.

The script set your Google Maps terms, FoodPanda URL, Instagram handles, and brand voice.

**To trigger:**
Staff member sends `check` in WhatsApp (internal mode). The agent scrapes all configured sources and classifies new reviews. For any review rated 3 stars or below, it drafts a reply and sends it to all owner phones as an alert.

The owner then replies to the alert:
- `post` — publish the AI-drafted reply
- `edit <new text>` — replace the draft, then post
- `ignore` — skip this review, no reply sent

**Automated:** The reputation script (`scripts/reputation_live.py`) can also be scheduled as a cron job to run sweeps without a staff trigger. On Railway, add a separate cron service or use APScheduler.

---

### Customer loyalty agent

**Status after script:** Ready if `CUSTOMER_CONFIG` was filled in. Skip if not.

No further action needed — the agent starts working automatically when any non-staff member messages the store's WhatsApp number.

**Customer flow:**
1. Customer scans the restaurant's QR code or messages the number directly
2. Agent greets them and registers them in the loyalty programme
3. Staff issue stamps by sending `stamp <customer-phone>` from internal mode
4. When `stamp_goal` is reached, the customer receives a reward code

**To verify it works:**
Message the store number from a number not in the MEMBERS list. You should get the loyalty greeting.

---

### Integrity agent (POS audit)

**Status after script:** Not ready — requires CSV file upload.

The integrity agent analyses POS transaction data to detect voids, comps, discount abuse, and margin leakage. It needs the restaurant's actual sales data to work.

**Activating via WhatsApp CSV upload:**

A whitelisted staff member sends three CSV files from their WhatsApp, one at a time. Each file needs a caption so the server knows what it contains.

**File 1 — Sales data** (caption: `sales`)

One row per line item in every order. Export this from the POS as a "sales detail report."

Minimum required columns:
```
order_id, datetime, staff_id, staff_name, item_sku, item_name, category,
qty, unit_price, line_amount, discount_amount, is_void, void_after_fire,
is_comp, order_status, payment_method, payment_amount, tax_rate
```

Example row:
```
ORD001,2026-06-01 13:42:00,S03,Ali,MN12,Smash Burger,Main,2,850,1700,0,0,0,0,closed,cash,1700,0
```

**File 2 — Menu data** (caption: `menu`)

One row per menu item with cost and selling price.

Required columns: `sku, name, category, cost, price`

```
MN12,Smash Burger,Main,350,850
DR05,Coke,Drink,40,150
```

**File 3 — Staff data** (caption: `staff`)

One row per employee.

Required columns: `staff_id, name, role`

```
S03,Ali,Cashier
S07,Sara,Cashier
S01,Ahmed,Manager
```

**How to send each file:**
1. Open WhatsApp and go to the store's number
2. Tap the attachment icon and select the CSV file from your phone's files
3. Before sending, tap the caption field and type `sales` (or `menu` or `staff`)
4. Send

After each upload you receive a confirmation: `Saved sales data (847 rows). Type 'summary' to run a POS audit.`

Once all three files are uploaded, send `summary` from internal mode to run the first audit.

**Note on column names:** If the POS exports use different column names (e.g., `order_number` instead of `order_id`), a custom mapping file needs to be created at `app/ingest/mappings/<pos_name>.py` and the `mapping` field in the POS connection updated. The default mapping (`cafe_generic`) works for any POS that can be configured to use those column names.

**Re-uploading data:**
Send the same file again with the same caption to replace it. The server always uses the most recently uploaded version. Cache is invalidated automatically so the next `summary` command re-runs the analysis on the new data.

---

### Revenue agent

**Status after script:** Not ready — requires configuration.

The revenue agent provides strategic revenue advice: pricing recommendations, category performance, campaign ROI, and upsell opportunities.

Configure it via the API after the store is created:

```bash
curl -X POST https://your-railway-domain.up.railway.app/admin/stores/{id}/revenue \
  -H "Content-Type: application/json" \
  -d '{
    "data_dir": null,
    "db_path": ":memory:",
    "config": {}
  }'
```

For the revenue agent to give meaningful output it needs access to aggregated sales data. Providing the same sales CSV used by the integrity agent (as a `data_dir` path or via the config) is the fastest path to activation. Full revenue agent data source configuration is covered separately.

Once configured, staff trigger it with commands like `revenue`, `sales`, `pricing`, or `strategy` in internal mode.

---

## Step 4 — Verify everything is working

**Health check:**
```
GET https://your-railway-domain.up.railway.app/health
```

Returns `"status": "ok"` when DB, Twilio, Apify, and all agent imports are healthy. Returns `"status": "degraded"` with per-component error details if anything is wrong.

**Full end-to-end test per agent:**

| Agent | Test command | Expected response |
|---|---|---|
| Scout | Send `scout` (internal mode) | Competitive intelligence report, 2–5 min |
| Reputation | Send `check` (internal mode) | Review summary or "No new reviews found" |
| Integrity | Send `summary` after CSV upload | Executive summary with leakage estimate |
| Revenue | Send `revenue` (internal mode) | Revenue advice or "not configured" message |
| Customer | Message from a non-staff number | Loyalty greeting |

---

## Troubleshooting

**Staff member gets customer greeting instead of staff tools**
Their WhatsApp number is not in the MEMBERS list. Add them via:
```bash
curl -X POST .../admin/stores/{id}/members \
  -d '{"phone": "whatsapp:+923001234567", "role": "staff"}'
```

**Scout returns "no competitors found"**
Competitors were not seeded. Re-run the onboarding script with the COMPETITORS sections filled in, or call `POST /admin/stores/{id}/seed-competitors`.

**Integrity returns "POS not configured"**
The CSV files have not been uploaded yet. Follow the CSV upload steps above.

**CSV upload returns "Could not detect file type"**
The caption was missing or did not contain a recognisable keyword. Resend the file with caption `sales`, `menu`, or `staff`. The auto-detection from column headers requires the file to have at least one column matching the expected mapping.

**Review alert not received by owner**
Check that the owner's number is in `MEMBERS` with `role: "owner"` AND in `CUSTOMER_CONFIG.owner_phones`. The reputation agent reads both.

**403 on `/whatsapp` webhook**
Twilio signature validation is disabled by default (`TWILIO_VALIDATE_SIGNATURE=false`). If it is enabled and returning 403, the Railway reverse proxy is rewriting the URL before validation. Leave it disabled until the proper X-Forwarded headers fix is implemented.
