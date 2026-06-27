"""Offline demo of the Maître d' — no server, no network, no Claude key needed.

Walks a few WhatsApp conversations through the decision engine (using the
deterministic NLU fallback) and prints what the guest would receive plus any
internal staff alerts / outbound messages.

    python -m scripts.maitre_d_demo
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig
from app.maitre_d.store import Store

# A movable clock so the demo can fast-forward time for sweeps. Starts Mon 15
# Jun 2026, 11:00 (the Friday that week is the 19th).
CLOCK = {"t": datetime(2026, 6, 15, 11, 0)}


def show(md: MaitreD, phone: str, text: str, who: str = "") -> None:
    label = who or phone
    print(f"\n  {label} ▶  {text}")
    reply = md.handle_message(phone, text, profile_name=who)
    print(f"  Maître d' ◀  {reply.text}")
    print(f"             [intent={reply.intent} action={reply.action} "
          f"vip={reply.is_vip} risk={reply.no_show_band or '-'}]")
    if reply.staff_alert:
        print(f"             ⚑ STAFF: {reply.staff_alert}")
    for to, msg in reply.outbound:
        print(f"             ✉  → {to}: {msg}")


def main() -> None:
    # A deliberately small venue (two 2-tops + one 4-top) so the waitlist fills.
    config = VenueConfig.load()
    config.tables = [("T1", 2), ("T2", 2), ("W1", 4)]
    md = MaitreD(store=Store(":memory:"), config=config,
                 client=None, now_fn=lambda: CLOCK["t"])

    print("=" * 70)
    print("1) A normal guest books over a couple of messages")
    print("=" * 70)
    guest = "+923009990000"
    show(md, guest, "Hi there", who="Omar")
    show(md, guest, "I'd like a table for 2 this Friday at 8pm", who="Omar")

    print("\n" + "=" * 70)
    print("2) A VIP is recognised the moment they book")
    print("=" * 70)
    vip = "+923001112222"  # Ayesha Khan (food critic) from the default VIP list
    show(md, vip, "table for 4 friday 8pm, window seat please", who="")

    print("\n" + "=" * 70)
    print("3) Slot fills up → next guest is waitlisted")
    print("=" * 70)
    show(md, "+923002", "table for 2 friday 8pm", who="Guest")       # takes last 2-top
    show(md, "+923008887777", "table for 2 friday 8pm", who="Latecomer")  # waitlisted

    print("\n" + "=" * 70)
    print("4) A cancellation frees a table → the waitlist is offered it")
    print("=" * 70)
    show(md, guest, "cancel", who="Omar")
    # The latecomer can now accept the freed table.
    show(md, "+923008887777", "yes", who="Latecomer")

    print("\n" + "=" * 70)
    print("5) A high-no-show-risk guest is held pending a deposit, then pays")
    print("=" * 70)
    risky = "+923005550000"
    # Seed two prior no-shows so this booking scores high-risk.
    from app.maitre_d.models import Reservation
    for i in range(2):
        md.store.add_reservation(Reservation(
            reservation_id=f"seed{i}", phone=risky, party_size=2,
            when=datetime(2026, 5, 1, 20, 0), status="no_show"))
    show(md, risky, "table for 4 saturday 8pm, it's Hasan", who="Hasan")
    show(md, risky, "yes", who="Hasan")            # → secure payment link
    res = md.store.latest_active_reservation_for(risky)
    reply = md.handle_payment_webhook({"ref": res.payment_ref, "status": "paid"})
    print(f"             💳 payment webhook → {reply.text}")

    print("\n" + "=" * 70)
    print("6) Day-before reminders go out automatically")
    print("=" * 70)
    CLOCK["t"] = datetime(2026, 6, 18, 20, 0)      # ~24h before Friday dinner
    result = md.send_due_reminders()
    print(f"  Maintenance tick: {result.reminders_sent} reminder(s) sent")
    for to, msg in result.outbound:
        print(f"             ✉  → {to}: {msg}")

    print("\n" + "=" * 70)
    print("7) The door: check-ins, auto-complete and no-show sweep")
    print("=" * 70)
    CLOCK["t"] = datetime(2026, 6, 19, 20, 5)       # Friday, just after service
    seated = md.store.list_reservations(status="confirmed")
    if seated:
        md.mark_seated(seated[0].reservation_id)
        print(f"  Floor marked {seated[0].name or seated[0].phone} as seated.")
    CLOCK["t"] = datetime(2026, 6, 19, 22, 30)       # later that night
    sweep = md.run_maintenance()
    print(f"  Maintenance tick: completed={sweep.completed} no_shows={sweep.no_shows} "
          f"offers_expired={sweep.offers_expired} deposits_expired={sweep.deposits_expired}")
    for alert in sweep.staff_alerts:
        print(f"             ⚑ STAFF: {alert}")

    print("\n" + "=" * 70)
    print("Reservations on the book:")
    for r in md.store.list_reservations():
        print(f"  • {r.when:%a %d %b %I:%M%p}  party {r.party_size}  "
              f"table {r.table_id}  {r.status}  vip={r.is_vip}  risk={r.no_show_band}")


if __name__ == "__main__":
    main()
