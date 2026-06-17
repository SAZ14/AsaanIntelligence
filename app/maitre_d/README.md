# Maître d' Agent

Reservations and the door, over WhatsApp. The Maître d' **takes and confirms
bookings**, **runs the waitlist**, **predicts and cuts no-shows**, and
**recognises a VIP the moment they book** — all by chatting with guests on
WhatsApp. Claude understands the guest; deterministic code makes every booking
and door decision.

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
                                   │  • no-show risk → deposit/hold     │
                                   │  • waitlist + promotion on cancel │
                                   └──────────────────────────────────┘
                                                  │
   Guest's WhatsApp ◄── Twilio ◄── TwiML reply ◄──┤
                                                  └──► SQLite (reservations,
                                                       waitlist, guests, convo state)
```

### The conversation flow
1. Guest messages the venue's WhatsApp number.
2. **Claude parses** free text → intent + slots (party size, date/time, name,
   special requests). Falls back to a deterministic parser if no Claude key.
3. **Slot-filling**: if anything's missing, the agent asks for it and remembers
   the rest across messages (conversation state in SQLite).
4. Once complete, **code decides**:
   - **VIP?** Looked up by phone in the manual VIP list → greeted by name, flagged
     on the reservation, staff alerted with their notes.
   - **Table free?** Checks capacity (tables × turn time) for that slot.
     - Free → **booked** (or held **pending a deposit** if no-show risk is high).
     - Full → **waitlisted** (VIPs jump the queue).
   - **No-show risk** scored from history, lead time, party size, deposit, VIP.
5. **Reply** goes back to the guest; **cancellations promote the waitlist** (the
   next guest is offered the freed table and confirms with "YES").

---

## What the guest can do

| Message | Result |
|---|---|
| "Table for 4 this Friday 8pm" | books, or waitlists if full |
| "it's Ayesha" / "window seat please" | name + special requests captured |
| "yes" / "no" | confirms a hold or accepts/declines a waitlist offer |
| "cancel" | cancels their booking (and frees the table for the waitlist) |
| "change it to 9pm" | modifies the booking |

---

## Decisions in detail

- **VIP recognition** — `config.py` holds a manual VIP list (phone → name, tier,
  notes). Recognised the moment they book; staff get an alert with their notes
  (e.g. "food critic — offer the window table").
- **No-show prediction** (`noshow.py`) — a transparent heuristic score from prior
  no-shows, lead time, party size, weekend, deposit and VIP status. High-risk,
  non-VIP bookings are held **pending a deposit** to cut no-shows.
- **Waitlist** (`store.py` + `agent.py`) — when full, guests join the waitlist
  (VIPs first). On a cancellation the freed table is **offered to the next in
  line**; they confirm to convert it into a real booking.
- **Capacity** (`config.py`) — tables (by seats) + turn time + service windows,
  with sensible café defaults; override via a JSON config.

---

## Running it

### Serve the WhatsApp webhook
```bash
python -m scripts.maitre_d_serve        # app.maitre_d.api:app
```
Point your Twilio WhatsApp number's inbound webhook at `POST /webhook/whatsapp`.

### Environment
```
MAITRE_D_DB           SQLite path (default data/maitre_d.db)
MAITRE_D_CONFIG       venue config JSON (capacity + VIP list); optional
MAITRE_D_USE_CLAUDE   "1" to use Claude for NLU (needs ANTHROPIC_API_KEY)
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM   for outbound (waitlist offers)
```
Without Twilio it logs instead of sending; without Claude it uses the fallback
parser. Replies to inbound messages go back inline as TwiML (no creds needed).

### Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/whatsapp` | guest conversation |
| GET | `/reservations` | list reservations (optional `?status=`) |
| GET | `/waitlist` | list waitlist entries |
| GET | `/health` | status |

### Try it offline (no server / no key)
```bash
python -m scripts.maitre_d_demo
```
Walks booking → VIP → waitlist → cancel → promotion end to end.

---

## Configuration

`data/maitre_d_config.example.json` shows the shape: venue name, tables (id +
seats), service windows, turn times, max party size, and the VIP list. Copy and
edit per venue, then point `MAITRE_D_CONFIG` at it.

---

## Use the engine directly (library)
```python
from app.maitre_d.agent import MaitreD
from app.maitre_d.store import Store

md = MaitreD(store=Store(":memory:"))
reply = md.handle_message("+923001112222", "table for 2 friday 8pm, it's Ayesha")
print(reply.text)        # confirmation / waitlist / question
print(reply.is_vip, reply.no_show_band, reply.staff_alert)
```

---

## Code map

| File | Role |
|---|---|
| `agent.py` | decision engine — booking, waitlist, no-show, VIP, replies |
| `nlu.py` | Claude NLU + deterministic fallback parser |
| `noshow.py` | heuristic no-show risk scoring |
| `store.py` | SQLite (reservations, waitlist, guests, conversation state) |
| `config.py` | venue capacity, service windows, VIP list |
| `models.py` | Pydantic models (guest / reservation / waitlist) |
| `whatsapp.py` | Twilio inbound parsing + outbound client |
| `api.py` | FastAPI app exposing the webhook |

Tests: `tests/test_maitre_d.py`. Demo: `python -m scripts.maitre_d_demo`.

> Self-contained — this agent modifies no other agent in the repo.
