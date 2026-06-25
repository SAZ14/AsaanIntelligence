# AsaanIntelligence

**Turn a café's sales data into plain-English money advice — over WhatsApp.**

AsaanIntelligence reads a café's point-of-sale (POS) data and answers two
questions every owner cares about:

1. **"Where am I losing money?"** — staff theft, over-comping, discount abuse,
   customers who quietly stopped coming.
2. **"How do I make more?"** — which prices to nudge, what's selling, when
   you're slow, and concrete ways to grow revenue.

There are two ways an owner gets this:

| Product | What it is | Who it's for |
|---|---|---|
| **Revenue Agent** | A WhatsApp chatbot the owner texts like a friend | Day-to-day, "just ask it" |
| **Audit Tool** | An offline report that hunts for leakage & lost customers | Deep monthly check-up |

Both run on the **same café data** — no new hardware, no app to install for the owner.

---

## 1. The Revenue Agent (the main thing) — how an owner uses it

### In one picture

```
  Owner's phone                  Our one WhatsApp bot              The café's data
 ┌──────────────┐   texts a     ┌────────────────────┐  reads   ┌──────────────────┐
 │ "How did we  │ ───────────▶  │  AsaanPay Rev Agent │ ───────▶ │ sales_detail.csv │
 │  do this     │               │  (figures out WHO   │          │ menu.csv         │
 │  week?"      │ ◀───────────  │   is texting)       │ ◀─────── │ staff.csv        │
 └──────────────┘   replies     └────────────────────┘  answer  └──────────────────┘
        ▲                                                                  │
        └──────── answer first, then money-making advice ◀────────────────┘
```

**The owner does nothing technical.** They just save a WhatsApp contact
("AsaanPay Rev Agent") and text it questions in normal language. One bot serves
many cafés — when an owner texts, the system looks at *their phone number* and
pulls up *their café's* data only. No café can ever see another café's numbers.

### What a real conversation looks like

> **Owner ▶** How did we do this week?
>
> **Agent ◀** Sugar Rush — the last 7 days (25 May – 31 May)
> Revenue: **PKR 958,898** across 762 orders (avg ticket PKR 1,258). Best seller: Cafe Latte (113 sold).
>
> 💰 **Quick price wins** (sells well → small bump):
> • Avocado Toast sells ~294/mo — raise PKR 50 (1,450→1,500) = **+PKR 14,259/mo**
> • Cafe Latte sells ~485/mo — raise PKR 10 (580→590) = **+PKR 4,704/mo**
>
> 📈 **Then bigger plays:**
> 1. Fill the late-night lull (~PKR 456,291/mo) — host acoustic nights / evening menu.
> 2. Loyalty card + win back 19 lapsed regulars (~PKR 353,513/mo).

> **Owner ▶** What can I raise prices on?
>
> **Agent ◀** • Avocado Toast sells ~321/mo — raise PKR 50 = +PKR 15,568/mo
> • Cafe Latte sells ~484/mo — raise PKR 10 = +PKR 4,695/mo
> *(…about PKR 39,838/mo extra from these small raises.)*
> • Consider **lowering** Cheesecake: priciest bakery item and slow — a small cut may move more.

**The golden rule:** every answer ends with advice, never just numbers. First
cheap wins (price tweaks), then bigger plays (loyalty, slow-hour campaigns, menu fixes).

### Things the owner can text

| The owner asks… | They get back… |
|---|---|
| "How did we do this week?" | revenue, orders, average ticket, best seller |
| "Best sellers this month" | top products by sales and by profit |
| "What can I raise prices on?" | exact price moves + extra rupees/month |
| "When are we slow?" | quietest days and time windows |
| "Campaign ideas" | brand-safe ways to fill those quiet hours |
| "How do I grow revenue?" | the full game plan |
| "Give me menu advice" | push the winners, fix/cut the losers, add gaps |
| "How do I get repeat customers?" | loyalty + win-back plan |
| "Send me a weekly digest" | automatic updates, no asking needed |

### Try it right now (no phone, no setup)

This runs the agent against the built-in sample café and prints a full chat:

```bash
pip install -e .
python -m scripts.revenue_demo
```

---

## 2. The Audit Tool — the deep money check-up

Where the chatbot is for everyday questions, the audit tool is the monthly
"where is money leaking?" report. It reads the same sales file and flags:

- **Theft & leakage** — the classic *serve → take cash → void the order* trick,
  plus staff who comp or discount way above everyone else. It ranks staff and
  estimates the monthly loss in rupees.
- **Lost customers** — regulars who came often, then went quiet. These are your
  win-back targets, with their lost value attached.
- **Operations** — your busy vs. dead hours, cash vs. card split, and best/worst
  items by profit margin.

Run a report:

```bash
python -m scripts.integrity_report        # leakage / theft audit
python -m scripts.retention_ops_report    # lost customers + operations
```

The shipped sample data (`data/`) has problems **deliberately planted** so you
can confirm the engine works — e.g. staff `S03` is the planted thief leaking
~PKR 61k/month. See `data/DATASET_README.md` for the full "answer key."

---

## How a café gets set up (the owner's onboarding)

The owner never touches any of this — it's a one-time setup done for them:

1. **Onboard the café** (one command) — creates a private data folder for that
   café and links the owner's phone number to it:
   ```bash
   python -m scripts.revenue_add_cafe --id sip --name "SIP" --owner +923331112222
   ```
2. **Café's POS data lands in that folder** — exported nightly (sales, menu, staff).
3. **Owner saves the WhatsApp contact** and starts texting. Done.

That's it. From the owner's side it's "save a number, ask it stuff."

---

## Running the live service (for operators)

```bash
python -m scripts.revenue_multi_serve     # one bot for all cafés, on :8002
```

Point your Twilio WhatsApp number's inbound webhook at `POST /webhook/whatsapp`.

| Endpoint | Purpose |
|---|---|
| `POST /webhook/whatsapp` | owner Q&A (auto-routed by sender's number) |
| `POST /digests/{cadence}` | cron pushes daily/weekly/monthly digests |
| `GET /cafes` | list onboarded cafés |
| `GET /health` | status check |

**Environment knobs** (all optional):
```
REVENUE_TENANTS      registry of which phone → which café
REVENUE_USE_CLAUDE   "1" to use Claude to understand messages (needs ANTHROPIC_API_KEY)
TWILIO_*             the bot's WhatsApp credentials
```
Without Twilio it just logs replies; without a Claude key it uses a built-in
keyword parser — so it works fully offline for testing.

---

## Your data stays your data

Privacy is enforced **three times over**, because owners are trusting us with
their books:

1. **Onboarding** gives each café its own private folder — never the shared sample.
2. **Startup** refuses to run if two cafés would share a folder or database.
3. **Runtime** — each café's agent only ever reads its own folder. No code pools
   data across cafés.

> Sugar Rush for Sugar Rush, SIP for SIP.

---

## What's in the box

```
app/
  revenue/      the WhatsApp Revenue Agent (chatbot + advice engine)
  analysis/     the audit engine (leakage + retention)
  ingest/       loads messy POS exports into a clean shape
  report/       renders audit reports
scripts/        run things: demo, reports, the live server, onboarding
data/           a realistic sample café + its "answer key"
tests/          test suite (run with: pytest)
```

Deeper docs:
- **`app/revenue/README.md`** — the Revenue Agent's full architecture & code map.
- **`data/DATASET_README.md`** — the sample dataset and what the engine should find.

---

## Quick start, end to end

```bash
pip install -e .              # install
pytest                        # confirm everything works
python -m scripts.revenue_demo        # see the chatbot in action
python -m scripts.integrity_report    # see the leakage audit
```
