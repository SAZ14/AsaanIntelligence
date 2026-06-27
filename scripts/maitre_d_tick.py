"""Run one Maître d' maintenance pass, then exit. Cron-friendly.

Expires stale waitlist offers, sweeps the door (auto-complete / no-show /
release unpaid holds) and sends day-before reminders, dispatching any outbound
WhatsApp messages via the configured Twilio client (log-only without creds).

    python -m scripts.maitre_d_tick

The long-running API (app.maitre_d.api) also ticks on a timer; use this script
when you'd rather drive it from cron / a scheduler instead.
"""

from __future__ import annotations

import os

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig
from app.maitre_d.store import Store
from app.maitre_d.whatsapp import WhatsAppClient


def main() -> None:
    store = Store(os.environ.get("MAITRE_D_DB", "data/maitre_d.db"))
    config = VenueConfig.load(os.environ.get("MAITRE_D_CONFIG"))
    md = MaitreD(store=store, config=config)
    wa = WhatsAppClient()

    result = md.run_maintenance()
    for phone, text in result.outbound:
        wa.send(phone, text)
    for alert in result.staff_alerts:
        print(f"[staff-alert] {alert}")

    print(
        f"tick: offers_expired={result.offers_expired} "
        f"no_shows={result.no_shows} completed={result.completed} "
        f"deposits_expired={result.deposits_expired} "
        f"reminders_sent={result.reminders_sent} "
        f"messages={len(result.outbound)}"
    )


if __name__ == "__main__":
    main()
