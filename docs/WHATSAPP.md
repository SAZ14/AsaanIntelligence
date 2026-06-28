# Reputation → WhatsApp wiring

The Reputation agent classifies reviews (Haiku), reconstructs the visit, and
drafts a reply (Sonnet). This module pushes each review that needs attention to
the venue owner on WhatsApp and routes the owner's reply back via a webhook.

**Live delivery uses Twilio's WhatsApp API** (sandbox or full sender). The Meta
Cloud API interactive-button path is kept as an alternative backend.

```
reviews ─► reputation agent ─► notifier.send_review_alert ─► Twilio ─► owner's phone
                                                                          │ replies POST/EDIT/IGNORE
owner's phone ───────────────► POST /twilio/inbound (webhook) ◄───────────┘
```

> **Why keyword replies, not buttons?** Twilio's WhatsApp **sandbox** can't send
> tappable quick-reply buttons (those need approved Content templates on a full
> sender). So the alert ends with `Reply *POST* / *EDIT* / *IGNORE*` and the
> webhook parses those keywords (free text → Q&A stub).

## 1. Configure

```bash
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY, TWILIO_*, OWNER_NUMBER
pip install -e .
```

| Var | Where it comes from |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic console |
| `TWILIO_ACCOUNT_SID` | Twilio console (starts `AC…`) |
| `TWILIO_AUTH_TOKEN` | Twilio console |
| `TWILIO_WHATSAPP_NUMBER` | Sandbox/sender number, e.g. `+14155238886` |
| `OWNER_NUMBER` | Your number, e.g. `+923001234567` |
| `DRY_RUN` | `true` prints request params; `false` actually sends |
| `TWILIO_VALIDATE_SIGNATURE` | `true` to verify `X-Twilio-Signature` on inbound (prod) |

**Join the sandbox first:** in the Twilio console, *Messaging → Try it out →
Send a WhatsApp message*, then send the join code (e.g. `join <word-word>`) from
your phone to the sandbox number. Twilio only delivers to numbers that joined.

## 2. Verify formatting offline (no Twilio calls)

`DRY_RUN=true` (default) prints the exact Twilio request params instead of sending:

```bash
DRY_RUN=true python scripts/reputation_live.py
```

## 3. Run the inbound webhook

```bash
uvicorn whatsapp.webhook:router --reload --port 8000
```

Routes:
- `POST /twilio/inbound` — owner's reply (POST / EDIT / IGNORE or free text)
- `GET|POST /webhook` — Meta Cloud API path (alternative backend)

## 4. Expose it publicly with ngrok

```bash
ngrok http 8000
# → Forwarding  https://<random>.ngrok-free.app -> http://localhost:8000
```

Inbound URL: `https://<random>.ngrok-free.app/twilio/inbound`

## 5. Paste the URL into the Twilio console

Twilio console → **Messaging → Try it out → WhatsApp sandbox settings** (for a
full sender: **Messaging → Senders → WhatsApp senders → your number**):

1. **"When a message comes in"**: `https://<random>.ngrok-free.app/twilio/inbound`
2. Method: **HTTP POST**
3. Save. Now replying POST/EDIT/IGNORE from your phone hits the webhook, which
   logs the routed action (`post_reply` is a stub that logs "would post to
   Google"; `ignore` logs; free text → Q&A stub) and replies with a short ack.

## 6. Send for real

```bash
DRY_RUN=false python scripts/reputation_live.py
```

Each flagged review arrives on your phone; reply `POST` / `EDIT` / `IGNORE`.

## Tests

```bash
pytest tests/test_whatsapp.py -q   # Twilio send formatting, inbound parsing,
                                   # Meta payload + handshake — all DRY_RUN
```
