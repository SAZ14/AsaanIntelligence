# Loyalty Agent — Setup Guide (plain English)

This guide takes the loyalty agent from "works on a laptop" to "real customers
scanning at the counter." No deep tech background needed — just follow the
steps in order.

## The big picture (3 boxes)

```
  Customer's WhatsApp  ←→  Twilio  ←→  Your webhook + database
     (their phone)       (messenger)   (our code, on a server)
```

- **Twilio** is the middleman that connects WhatsApp to our code. You rent it.
- **The webhook** is our code, running on an always-on server. It counts stamps.
- **The database** (`loyalty.db`, a single SQLite file) remembers every
  customer's stamp count, looked up by their phone number.

**How a customer is recognised:** every WhatsApp message carries the sender's
phone number automatically. That number *is* their loyalty card — no signup, no
app, no card to lose.

---

## Part 1 — Test it on your own phone (≈20 min, free)

### 1. Create a Twilio account
Go to <https://www.twilio.com/try-twilio> and sign up.

### 2. Turn on the WhatsApp sandbox
In the Twilio Console: **Messaging → Try it out → Send a WhatsApp message.**
You'll see a sandbox number and a join code like `join purple-tiger`.
From your own WhatsApp, send that join code to the sandbox number. You're now
connected.

### 3. Run the webhook and expose it to the internet
On any computer with Python 3.11+:

```bash
pip install -e .[qr]
export RESTAURANTS_CONFIG=data/restaurants.example.json
export LOYALTY_DB=loyalty.db
uvicorn app.whatsapp.webhook:app --port 8000
```

In a second terminal, make it reachable from the internet with ngrok
(<https://ngrok.com>, free):

```bash
ngrok http 8000
```

ngrok prints a public URL like `https://abc123.ngrok.io`.

### 4. Tell Twilio where to send messages
Back in the Twilio sandbox settings, find **"When a message comes in"** and
paste your public webhook URL with `/whatsapp/inbound` on the end:

```
https://abc123.ngrok.io/whatsapp/inbound
```

Save.

### 5. Try it 🎉
From your phone, send any message to the sandbox number. You'll get the loyalty
reply back:

```
🎉 Welcome to Sugar Rush Rewards!
You earned your 1st stamp on your Loyalty card.
[▰▱▱▱▱] 1/5
```

Send again → `2/5`, and so on. That's the whole loop working.

---

## Part 2 — Generate the QR posters

```bash
python scripts/generate_qr.py --config data/restaurants.example.json --all
```

This writes one poster per restaurant into `qr_posters/`. Print them and put
them on the counter. (Each QR opens WhatsApp to that venue with the message
pre-filled — sending it is the scan.)

---

## Part 3 — Go live for real (production)

Two things change from the test setup.

### A. A real always-on server (instead of your laptop + ngrok)
The easiest path is **Render** (<https://render.com>):

1. Push this repo to GitHub (the `customer-agent` branch).
2. In Render: **New → Blueprint**, point it at the repo. It reads `render.yaml`,
   builds the `Dockerfile`, and gives you a permanent URL like
   `https://loyalty-webhook.onrender.com`.
3. `render.yaml` already mounts a **persistent disk at `/data`** and sets
   `LOYALTY_DB=/data/loyalty.db`, so your stamp counts survive restarts.
4. Set `RESTAURANTS_CONFIG` to your real venues file (see Part 4).

Your webhook URL becomes `https://loyalty-webhook.onrender.com/whatsapp/inbound`.

> Railway, Fly.io, or any VPS work too — anything that runs the Dockerfile and
> gives a public URL with a persistent disk for `loyalty.db`.

### B. A real WhatsApp number per restaurant (instead of the shared sandbox)
For routing to work, **each restaurant needs its own WhatsApp number.** In
Twilio: **Messaging → Senders → WhatsApp senders → request a sender.** Each goes
through Meta's business verification (Twilio guides you; allow a few days).

When approved, put each number into your `restaurants.json` and set every
number's **"when a message comes in"** webhook to your production
`/whatsapp/inbound` URL.

---

## Part 4 — Your venues file (`restaurants.json`)

One entry per restaurant. Copy `data/restaurants.example.json` and edit:

```json
[
  {
    "id": "sugar_rush",
    "name": "Sugar Rush",
    "whatsapp_number": "+14155238886",
    "stamps_required": 5,
    "reward": "a free ice cream"
  }
]
```

- `whatsapp_number` — that venue's WhatsApp number (the one customers message).
- `stamps_required` / `reward` — tweak freely per venue.

This file is gitignored (it's per-deployment). Adding a new restaurant = one new
entry here + its QR poster. No code changes.

---

## Part 5 — Staff: handing over rewards

Loyalty is per restaurant, so tell the tool which venue:

```bash
# Who has rewards waiting (all venues):
python scripts/customer_report.py

# Look one customer up by phone at a venue:
python scripts/customer_report.py --restaurant sugar_rush +923001234567

# Mark their reward as given:
python scripts/customer_report.py --restaurant sugar_rush +923001234567 --redeem
```

A customer who completed a card shows their unlocked reward; `--redeem` marks it
handed over.

---

## What it costs (rough, monthly)

| Thing | Cost |
|-------|------|
| SQLite storage | **free** (built into Python) |
| Server to run the webhook | **~$7/mo** (Render Starter with a persistent disk) |
| Twilio / WhatsApp messages | a few **fractions of a cent per scan** + small per-conversation fee |

Because the **customer messages first** (the scan), WhatsApp lets the business
reply for free for 24 hours — so the instant stamp reply needs **no template
approval.** That's the easy path, and the agent is already on it.

---

## Quick reference — environment variables

| Variable | What it does | Default |
|----------|--------------|---------|
| `RESTAURANTS_CONFIG` | path to your venues JSON | `restaurants.json` |
| `LOYALTY_DB` | path to the SQLite database | `loyalty.db` |
| `PORT` | port the webhook listens on (set by host) | `8000` |

The webhook needs nothing else — it replies via TwiML and does not call the
Twilio SDK.
