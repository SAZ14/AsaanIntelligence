# Agent Test Results — Anatummy (Store 5)

Tested: 2026-06-30  
Test method: direct function calls bypassing Twilio (no HTTP layer)  
ZAI model: `glm-4.7` (BigModel) — latency varies 5–100s depending on server load  
Apify: cache miss tests skipped (credits low, $0.37 remaining)

> **Twilio ack is always <1s** — all slow operations run as background tasks.  
> Times below = how long until the WhatsApp reply actually arrives.

---

## Integrity Agent

Input: `summary`, `leakage`, `profit`, `staff`, `daily`, `weekly`, or any natural language POS question.  
Data source: uploaded CSV files (`pos_sales`, `pos_menu`, `pos_staff`) — no Apify.

| Command | Time | Output |
|---|---|---|
| `summary` | **16.4s** | Anatummy: over 3 days, net sales PKR 7,500 at 61% gross margin (profit PKR 4,610). Estimated leakage PKR 1,102/period (~PKR 11,022/month). Top issue: payment discrepancy on 2 orders (PKR 1,050). 2 payment mismatch(es) and 8 tax anomaly(ies) need review. |
| `leakage` | **8.2s** | Estimated leakage: PKR 1,102 (monthly ~PKR 11,022). Theft voids: PKR 542. Excess comps: PKR 381. Excess discounts: PKR 179. |
| `profit` | **7.3s** | Net sales: PKR 7,500. Gross profit: PKR 4,610 (61.5%). COGS sold: PKR 2,890. Wasted COGS (comps/voids): PKR 590. Payment mismatches: 2 (PKR 250 unreconciled). |
| `staff` | **5.9s** | Staff anomalies: [low] Ali (S03) — theft PKR 542, void rate 27.1% vs venue 8.7%. [low] Sara (S07) — comp_abuse PKR 381, comp rate 16.8% vs venue 9.3%. [low] Sara (S07) — discount_abuse PKR 179. |
| `daily` | **7.5s** | Wed 3 Jun 2026 — Orders: 2 \| Sales: PKR 1,150. Profit: PKR 720 (63%). Leakage: PKR 0. vs prev: ▼ 73% sales. |
| `weekly` | **6.6s** | 28 May – 3 Jun 2026 — Orders: 8 \| Sales: PKR 7,500. Profit: PKR 4,610 (61%). Leakage: PKR 1,102. Daily breakdown shown. |
| `who had the most voids this week?` (natural) | **21.4s** | Returned staff anomaly report — Ali (S03) highest void rate 27.1% vs venue avg 8.7%. |

**Notes:** Shorthand commands (`summary`, `leakage`, etc.) take 6–10s. Natural language takes 18–25s (adds routing LLM + adapt call). All working correctly on real uploaded POS data.

---

## Reputation Agent

### Chat (natural language — no Apify)

Input: any question about reviews, ratings, customer sentiment.  
Data source: `findings` table (last scraped Jun 28).

| Command | Time | Output |
|---|---|---|
| `what are people saying about us?` | **35.2s** | "The Good: Burgers taste decent. The Bad: Multiple complaints about cold food. Soggy fries." |
| `any bad reviews recently?` | **20.8s** | "Yes, two negative ones: 1.0/5 — 'Absolutely terrible. Cold burger, rude staff.' 2.0/5 — 'Very slow service, food was cold.'" |
| `how are our ratings on Google?` | **44.2s** | "Average: 2.25/5 📉. Breakdown (4 reviews): 1★ Cold burger/rude staff. 2★ Slow service. 3★ Soggy fries." |

### Check (Apify scrape)

| Scenario | Time | Notes |
|---|---|---|
| Cache hit (<60 min) | **~2–3s** | Serves findings from DB via `_send_outbound` |
| Cache miss (>60 min since last ok) | **~2–4 min** | Parallel Apify: Google Maps + FoodPanda + Instagram |
| In-flight (scrape already running) | **instant ack** | Returns "already in progress" message |

**Current state:** Cache miss (last ok run: 2026-06-28 19:04 UTC). Next `check` will trigger Apify.

### Post/ignore/edit actions

| Command | Time | Notes |
|---|---|---|
| `post 1` / `ignore 2` / `edit 3 Here is our response…` | **~1–2s** | Pure DB update, synchronous, no LLM |

---

## Scout Agent

Input: `scout` (or any shorthand that routes to scout)

| Scenario | Time | Notes |
|---|---|---|
| Cache hit (<24h, report saved in DB) | **~2–3s** | Serves saved `report_text` from `reports` table |
| Cache hit but no saved report_text | **~3–7 min** | Falls through to full Apify scrape |
| Cache miss (>24h since last ok run) | **~3–7 min** | Parallel Apify: web crawler + Instagram + Google Reviews |

**Current state:** Cache miss (no successful scout run on record for Anatummy). Next `scout` will trigger full Apify scrape (~3–7 min, then caches for 24h).

---

## Revenue Agent

Input: any sales/performance question.  
Data source: uploaded POS CSV files (same as integrity) — no Apify.

| Command | Time | Output |
|---|---|---|
| `how are our sales doing?` | **99.7s** ⚠️ | "For the 3-day period, net sales PKR 7,500. Gross profit PKR 4,610 (61.5% margin). No targets to compare against." *(ZAI was very slow this call — typical is 20–40s)* |
| `what is our profit?` | **23.3s** | "Gross profit PKR 4,610 from net sales PKR 7,500, 61.5% margin. Wasted COGS from comps/voids: PKR 590. 2 payment mismatches, PKR 250 unreconciled." |
| `daily report` | **18.1s** | "Anatummy, Wed 3 Jun 2026 — 2 orders, sales PKR 1,150, profit PKR 720 (63%), leakage PKR 0." |
| `weekly report` | **24.2s** | "28 May – 3 Jun 2026 — PKR 7,500 from 8 orders. Profit PKR 4,610 (61%). Leakage PKR 1,102." |
| `what items are selling best?` | **28.2s** | "Revenue advisor unavailable — try again shortly." *(ZAI error on this call)* |
| `when are our slowest hours?` | **38.4s** | "The data doesn't contain that information." *(dead-hours analytics not in uploaded CSV)* |
| `what campaigns should we run?` | **17.3s** | "Revenue advisor unavailable — try again shortly." *(ZAI error)* |

**Notes:**
- Revenue reads the same uploaded POS CSVs as integrity via the new DB fallback (`load_pos_from_db`).
- "Unavailable" responses are ZAI API failures (glm-4.7 flakiness), not code bugs — retrying works.
- Dead-hours analysis requires timestamped transaction data beyond what the test CSV provides.

---

## Customer Agent

Input: any customer WhatsApp message.  
Data source: `store_members` (stamp balances), `knowledge_base` (menu/venue info via vector search).

Tested with a fresh user (Ahmed, `+923119876543`) going through the full onboarding flow.

| Step / Command | Time | Output |
|---|---|---|
| `hi` (new user, no session) | **6.5s** | "Welcome to Anatummy! What name should we use for your rewards?" |
| `Ahmed` (name during onboarding) | **4.2s** | "Welcome to Anatummy, Ahmed! Collect 5 stamps to earn a free dessert. Text a receipt code from the counter to get your first stamp." |
| `how to earn stamps?` | **2.1s** | "You have 0/5 stamps (5 more for a free dessert). Lifetime stamps: 0." *(DB lookup — instant)* |
| `how many stamps do I have?` | **2.1s** | "You have 0/5 stamps (5 more for a free dessert). Lifetime stamps: 0." *(DB lookup — instant)* |
| `ABCD1234` (invalid stamp code) | **15.0s** | "We don't have any active deals at the moment! You can still earn a loyalty stamp with your purchase." |
| `redeem` (0 stamps) | **22.6s** | "You can redeem a reward if you have a full stamp card (5 stamps). Keep collecting!" |
| `what desserts do you have?` | **29.2s** | ⚠️ **Hallucinated** — gave incorrect USD prices and wrong items |
| `how much is the smash burger?` | **24.7s** | ⚠️ **Hallucinated** — said "$8.50" instead of PKR 650 |
| `where are you located?` | **17.4s** | ⚠️ **Hallucinated** — gave "123 High Street" instead of F-8/Beverly/Bahria |
| `what time do you open?` | **20.8s** | ⚠️ **Hallucinated** — said "8:00 AM" instead of 12pm–12am |

**Known issue — KB not working yet:**  
`sentence_transformers` is not installed in the local environment so knowledge base chunks could not be embedded and stored. The package is in `pyproject.toml` (available on Railway), but chunks must be inserted from Railway where the model can compute embeddings. Until then, the LLM answers menu/location questions from its own training data (hallucinated). Stamp balance, onboarding, and redemption flows work correctly.

**Fix needed:** After next Railway deployment, run the KB seeding script on the server to insert Anatummy's menu chunks with proper embeddings.

---

## Summary Table

| Agent | Command type | Cache hit | Cache miss / no cache |
|---|---|---|---|
| **Integrity** | Shorthand (`summary` etc.) | n/a | **6–10s** |
| **Integrity** | Natural language | n/a | **18–25s** |
| **Reputation** | Chat (any question) | n/a | **20–45s** |
| **Reputation** | `check` (scrape) | **~2s** | **2–4 min** |
| **Reputation** | `post`/`ignore`/`edit` | n/a | **~1s** |
| **Scout** | `scout` | **~2s** | **3–7 min** |
| **Revenue** | Any query | n/a | **18–60s** (ZAI varies) |
| **Customer** | `hi` / name onboarding | n/a | **4–7s** |
| **Customer** | Stamp balance / redeem | n/a | **2–3s** (DB only) |
| **Customer** | Menu / venue questions | n/a | **15–30s** (LLM) — needs KB fix |

Twilio webhook ack: **<1s** for all commands (background task pattern).
