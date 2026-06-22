# Twilio-hosted loyalty (no server)

This is the **zero-server** version of the loyalty agent. The code runs **on
Twilio** (Twilio Functions) and the stamp cards are stored **on Twilio** (Twilio
Sync). There is no Render, no ngrok, and no server for you to run or babysit —
everything lives in your Twilio account.

```
Customer scans QR → WhatsApp → Twilio runs inbound.js → saves card in Sync → replies
```

- `functions/inbound.js` — handles each scan: adds a stamp, saves to Sync, replies.
- `functions/winback.protected.js` — proactive "we miss you" via WhatsApp template
  (trigger it on a schedule with a free cron service).

The Python project in the repo root is still useful for **local testing**, the
**QR poster generator** (`scripts/generate_qr.py`), and as the reference
implementation — but for production you only need what's in this folder.

---

## One-time setup

### 1. Install the tools (once)
```bash
npm install -g twilio-cli
twilio plugins:install @twilio-labs/plugin-serverless
cd twilio-loyalty
npm install
```

### 2. Create a Sync service (the database)
```bash
twilio api:sync:v1:services:create --friendly-name loyalty
```
Copy the returned `ISxxxx…` SID.

### 3. Configure
```bash
cp .env.example .env
# fill in ACCOUNT_SID, AUTH_TOKEN, SYNC_SERVICE_SID, and RESTAURANTS
```

### 4. Deploy (gives you the URLs)
```bash
twilio serverless:deploy
```
Twilio prints your function URLs, e.g.
`https://loyalty-1234.twil.io/inbound`.

### 5. Point WhatsApp at it
For each restaurant's WhatsApp number (or the sandbox), set
**"When a message comes in"** → your `/inbound` URL.

### 6. Generate + print the QR posters (from the repo root, Python)
```bash
python scripts/generate_qr.py --number +14155238886 --venue "Sugar Rush"
```

That's it — customers can scan anytime; Twilio handles everything.

---

## Win-back (optional, later)

`winback.protected.js` sends the "we miss you" WhatsApp template to loyal
customers who've gone quiet. It needs:

1. An **approved WhatsApp template** with `{{1}} {{2}} {{3}}` (venue, days,
   reward) — copy its **ContentSid** into each venue's `winback_template_sid`.
2. A **schedule** — Twilio Functions have no built-in cron, so point a free
   scheduler (e.g. https://cron-job.org) at your `/winback` URL, say once a day.

---

## Staff: handing over rewards

A completed card has `pending_reward: true` in its Sync item. Until a staff
lookup function is added, view a customer's card in the Twilio Console →
**Sync → Maps → `cards_<venue>` → the customer's phone number**.
