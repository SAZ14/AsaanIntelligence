# Reputation → WhatsApp wiring

The Reputation agent classifies reviews (Haiku), reconstructs the visit, and
drafts a reply (Sonnet). This module pushes each review that needs attention to
the venue owner on WhatsApp as an **interactive** message with three buttons
— *Post reply*, *Edit*, *Ignore* — and handles the owner's tap via a webhook.

```
reviews ─► reputation agent ─► notifier.send_review_alert ─► WhatsApp (owner)
                                                                  │ taps a button
owner's phone ─────────────────► webhook (GET verify / POST) ◄────┘
```

## 1. Configure

```bash
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY, WHATSAPP_*, OWNER_NUMBER
pip install -e .
```

Key vars (see `.env.example`):

| Var | Where it comes from |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic console |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta App → WhatsApp → API Setup |
| `WHATSAPP_TOKEN` | Meta App → WhatsApp → API Setup (temp or system-user token) |
| `WHATSAPP_VERIFY_TOKEN` | **You invent it.** Must match what you paste into Meta. |
| `OWNER_NUMBER` | Owner's number, international format, no `+` (e.g. `923001234567`) |
| `DRY_RUN` | `true` prints payloads; `false` actually sends |

## 2. Verify formatting offline (no API calls)

`DRY_RUN=true` (the default) prints the exact Cloud API payload instead of
sending it:

```bash
DRY_RUN=true python scripts/reputation_live.py
```

## 3. Run the webhook

```bash
uvicorn whatsapp.webhook:router --reload --port 8000
# (or mount whatsapp.webhook.router on your own FastAPI app)
```

The webhook serves:
- `GET /webhook` — Meta verification handshake (echoes `hub.challenge`)
- `POST /webhook` — inbound button taps / free text

## 4. Expose it publicly with ngrok

Meta needs a public HTTPS URL.

```bash
ngrok http 8000
# → Forwarding  https://<random>.ngrok-free.app -> http://localhost:8000
```

Your callback URL is `https://<random>.ngrok-free.app/webhook`.

## 5. Register the callback in the Meta dashboard

Meta App dashboard → **WhatsApp → Configuration → Webhook → Edit**:

1. **Callback URL**: `https://<random>.ngrok-free.app/webhook`
2. **Verify token**: the exact value of `WHATSAPP_VERIFY_TOKEN` from your `.env`.
3. Click **Verify and save** — Meta sends `GET /webhook?hub.mode=subscribe&...`
   and our handler echoes `hub.challenge`. A green check means it worked.
4. Under **Webhook fields**, subscribe to **`messages`** so button taps and
   inbound texts are delivered.

## 6. Send for real

```bash
DRY_RUN=false python scripts/reputation_live.py
```

Tap a button on the owner's phone and watch the webhook log the routed action
(`post_reply` is a stub to be wired to Google later; `ignore` logs; free text
goes to the Q&A stub).

## Tests

```bash
pytest tests/test_whatsapp.py -q   # payload shape, handshake, button parsing — all DRY_RUN
```
