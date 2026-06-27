# Maître d' Agent

Reservations and the door, over WhatsApp. The Maître d' **takes and confirms
bookings**, **runs the waitlist**, **predicts and cuts no-shows** (holding risky
tables pending a **paid deposit**), **sends day-before reminders**, **works the
door** (check-ins, auto-complete, no-show sweep) and **recognises a VIP the
moment they book** — all by chatting with guests on WhatsApp. Claude understands
the guest; deterministic code makes every booking and door decision.

---

## How it works

```
   Guest's WhatsApp ───► Twilio ───► POST /webhook/whatsapp ───► MaitreD engine
                                                                      │
                            Claude NLU: "table for 4 Fri 8pm, it's Ayesha"
                                                                      │
                                   ┌──────────────────────────────────┤
                                   │ decisions (all in code):          │
                                   │  • availability vs capacity       │
                                   │  • VIP recognition (manual list)  │
                                   │  • no-show risk → deposit/hold    │
                                   │  • waitlist + promotion on cancel │
                                   └──────────────────────────────────┘
                                                  │
   Guest's WhatsApp ◄── Twilio ◄── TwiML reply ◄──┤
                                                  └──► SQLite (reservations,
                                                       waitlist, guests, convo state)

   payment gateway ──► POST /webhook/payment ──► confirm held table
   cron / timer ─────► POST /tasks/tick ───────► expire offers · door sweep · reminders
   floor / host ─────► POST /reservations/{id}/{seat|complete|no_show}
```

### The conversation flow
1. Guest messages the venue's WhatsApp number.
2. **Claude parses** free text → intent + slots (party size, date/time, name,
   special requests). Falls back to a deterministic parser if no Claude key.
3. **Slot-filling**: if anything's missing, the agent asks for it and remembers
   the rest across messages (conversation state in SQLite, forgotten after a TTL
   if abandoned).
4. Once complete, **code decides**:
   - **VIP?** Looked up by phone in the manual VIP list → greeted by name, flagged
     on the reservation, staff alerted with their notes.
   - **Table free?** Checks capacity (tables × turn time) for that slot.
     - Free → **booked** (or held **pending a deposit** if no-show risk is high).
     - Full → **waitlisted** (VIPs jump the queue).
   - **No-show risk** scored from history, lead time, party size, deposit, VIP.
5. **Reply** goes back to the guest; **cancellations promote the waitlist** (the
   next guest is offered the freed table and confirms with "YES").
6. **Out of band**, a maintenance tick keeps the book honest: it **expires stale
   waitlist offers** (rolling them to the next guest), **sends day-before
   reminders**, **auto-completes** finished visits, **marks no-shows** and
   **releases unpaid deposit holds**. No-shows feed straight back into step 4's
   risk scoring.

---

## What the guest can do

| Message | Result |
|---|---|
| "Table for 4 this Friday 8pm" | books, or waitlists if full |
| "it's Ayesha" / "window seat please" | name + special requests captured |
| "yes" / "no" | confirms a hold/deposit or accepts/declines a waitlist offer |
| "cancel" | cancels their booking (and frees the table for the waitlist) |
| "change it to 9pm" | modifies the booking (old table only freed once the new one is secured) |

---

## Decisions in detail

- **VIP recognition** — `config.py` holds a manual VIP list (phone → name, tier,
  notes). Recognised the moment they book; staff get an alert with their notes
  (e.g. "food critic — offer the window table").
- **No-show prediction** (`noshow.py`) — a transparent heuristic score from prior
  no-shows, lead time, party size, weekend, deposit and VIP status. High-risk,
  non-VIP bookings are held **pending a deposit**. Real outcomes (seated /
  completed / no-show) are recorded at the door, so the history signal is live —
  a guest who no-shows scores higher next time.
- **Deposits & payments** (`payments.py`) — on a high-risk hold the guest replies
  YES, gets a **secure checkout link**, and the table stays *pending* until the
  gateway's webhook confirms payment → *confirmed*. The provider is pluggable
  (`PaymentProvider`); a `StubPaymentProvider` runs the whole flow offline, and a
  real gateway (Stripe, a local PSP) drops in without touching the engine.
- **Reminders** — confirmed bookings get one day-before WhatsApp reminder
  (`reminder_lead_hours`), sent by the maintenance tick and de-duplicated so it
  never double-fires.
- **Waitlist** (`store.py` + `agent.py`) — when full, guests join the waitlist
  (VIPs first). On a cancellation the freed table is **offered to the next in
  line**; they confirm to convert it into a real booking. An unanswered offer
  **expires after `offer_ttl_minutes`** and rolls on to the next guest.
- **The door** — staff endpoints mark a guest **seated**, **completed** or
  **no_show**; the sweep auto-completes seated tables past their turn and flags
  confirmed-but-absent guests after a grace period.
- **Capacity** (`config.py`) — tables (by seats) + turn time + service windows,
  with sensible café defaults; override via a JSON config.
- **Time** — the engine runs on the **venue's** wall clock (`timezone`), so
  "tonight 8pm" resolves correctly wherever the service is hosted.

---

## Running it

### Serve the WhatsApp webhook
```bash
python -m scripts.maitre_d_serve        # app.maitre_d.api:app
```
Point your Twilio WhatsApp number's inbound webhook at `POST /webhook/whatsapp`.
When `TWILIO_AUTH_TOKEN` is set the webhook **verifies Twilio's signature** and
rejects forged requests.

### Background maintenance
The API ticks automatically every `MAITRE_D_TICK_SECONDS` (default 60). To drive
it from cron instead, run a one-shot pass:
```bash
python -m scripts.maitre_d_tick         # expire offers · door sweep · reminders
```

### Environment
```
MAITRE_D_DB              SQLite path (default data/maitre_d.db)
MAITRE_D_CONFIG          venue config JSON (capacity + VIP list); optional
MAITRE_D_USE_CLAUDE      "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
MAITRE_D_PUBLIC_BASE_URL public https base for Twilio signature checks behind a proxy
MAITRE_D_TICK_SECONDS    in-process maintenance interval (default 60; 0 disables)
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM   for outbound + signature
```
Without Twilio it logs instead of sending; without Claude it uses the fallback
parser. Replies to inbound messages go back inline as TwiML.

### Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/whatsapp` | guest conversation |
| POST | `/webhook/payment` | deposit-gateway callback → confirm held table |
| POST | `/reservations/{id}/seat` | mark a guest seated (door) |
| POST | `/reservations/{id}/complete` | mark a visit completed (door) |
| POST | `/reservations/{id}/no_show` | mark a no-show (door) |
| POST | `/tasks/tick` | run one maintenance pass on demand |
| GET | `/reservations` | list reservations (optional `?status=`) |
| GET | `/waitlist` | list waitlist entries |
| GET | `/health` | status |

### Try it offline (no server / no key)
```bash
python -m scripts.maitre_d_demo
```
Walks booking → VIP → waitlist → cancel → promotion → deposit+payment →
reminders → door check-in/no-show sweep end to end.

---

## Configuration

`data/maitre_d_config.example.json` shows the shape: venue name, timezone,
tables (id + seats), service windows, turn times, max party size, currency and
**deposit amount**, the **offer / no-show / reminder / conversation timers**, and
the VIP list. Copy and edit per venue, then point `MAITRE_D_CONFIG` at it.

---

## Use the engine directly (library)
```python
from app.maitre_d.agent import MaitreD
from app.maitre_d.store import Store

md = MaitreD(store=Store(":memory:"))
reply = md.handle_message("+923001112222", "table for 2 friday 8pm, it's Ayesha")
print(reply.text)        # confirmation / waitlist / question
print(reply.is_vip, reply.no_show_band, reply.staff_alert)

# Background housekeeping (drive from a timer/cron):
result = md.run_maintenance()
print(result.reminders_sent, result.no_shows, result.offers_expired)
```

---

## Code map

| File | Role |
|---|---|
| `agent.py` | decision engine — booking, waitlist, no-show, VIP, door lifecycle, maintenance, replies |
| `nlu.py` | Claude NLU + deterministic fallback parser |
| `noshow.py` | heuristic no-show risk scoring |
| `payments.py` | pluggable deposit provider (`PaymentProvider` + offline stub) |
| `store.py` | SQLite (reservations, waitlist, guests, conversation state) |
| `config.py` | venue capacity, service windows, timers, venue clock, VIP list |
| `models.py` | Pydantic models (guest / reservation / waitlist) |
| `whatsapp.py` | Twilio inbound parsing, signature validation + outbound client |
| `api.py` | FastAPI app: webhooks, door endpoints, maintenance tick, scheduler |

Tests: `tests/test_maitre_d.py`. Demo: `python -m scripts.maitre_d_demo`.
Maintenance: `python -m scripts.maitre_d_tick`.

> Self-contained — this agent modifies no other agent in the repo.
