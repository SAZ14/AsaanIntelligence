# Inventory Agent — Setup & Multi-Venue Guide

The inventory agent tracks stock from POS sales and sends each restaurant owner
a daily WhatsApp report. **You write zero code per restaurant** — adding a venue
is config + that venue's data.

---

## One-time setup (per install)

1. **Install deps**
   ```bash
   pip install -e .          # installs pydantic, anthropic, twilio, etc.
   ```

2. **Twilio account** (shared across all venues)
   - Create a Twilio account and enable WhatsApp (sandbox for testing, or apply
     for an approved business number for production).
   - Copy `.env.example` → `.env` and fill in:
     - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`
   - Load it before running (e.g. `set -a; source .env; set +a`).

3. **Venue list**
   - Copy `venues.example.toml` → `venues.toml`.
   - Add one `[[venue]]` block per restaurant (name, data folder, owner number).

---

## Adding a restaurant (repeat per venue — no coding)

1. **Make its data folder**, e.g. `venues/burger_joint/`, with 6 CSVs:

   | file | what it holds | who maintains it |
   |---|---|---|
   | `menu.csv` | the menu (sku, name, price) | once, at setup |
   | `recipes.csv` | each item → its ingredients | once, at setup |
   | `ingredients.csv` | units, cost, reorder levels | once, at setup |
   | `staff.csv` | staff list | once, at setup |
   | `stock_receipts.csv` | deliveries as they arrive | **owner, daily** |
   | `sales_detail.csv` | POS sales export | **from the POS, daily** |

   (Copy the demo files in `data/` as templates.)

2. **Add it to `venues.toml`:**
   ```toml
   [[venue]]
   name = "The Burger Joint"
   data_dir = "venues/burger_joint"
   owner_whatsapp = "whatsapp:+923009998877"
   ```

That's the whole process. The same code now serves this venue too.

---

## Running

```bash
# Preview every venue's report without sending (safe):
python scripts/daily_whatsapp_report.py

# Send all venues for real:
python scripts/daily_whatsapp_report.py --send

# Just one venue:
python scripts/daily_whatsapp_report.py --venue "The Burger Joint" --send
```

### Schedule it (send every night at 11pm)
```cron
0 23 * * *  cd /path/to/AsaanPayEnterprise && set -a && . ./.env && set +a && python scripts/daily_whatsapp_report.py --send >> output/cron.log 2>&1
```

---

## How sales get in (the POS link)

Each venue's `sales_detail.csv` is the POS export. Two ways to keep it fresh:
- **Manual/export:** most POS systems export a daily sales CSV — drop it in the
  venue's folder before the cron runs.
- **Automated:** if the POS has an API, add a small per-POS fetch step that
  writes that same CSV. The column names live in `app/ingest/mappings/` so a new
  POS format just needs a new mapping, not new logic.

---

## Do I need a database?

Not to start. File-per-venue (folders of CSVs) is plenty for a handful to dozens
of restaurants. Move to a database when you want: many venues, multiple people
editing data at once, historical trends/dashboards, or a web UI. Because all
storage access goes through `app/ingest/loader.py`, swapping CSVs for a database
later is a localized change — the agent logic doesn't change.
