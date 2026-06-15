"""Offline demo of the Maître d' — no server, no network, no Claude key needed.

Walks a few WhatsApp conversations through the decision engine (using the
deterministic NLU fallback) and prints what the guest would receive plus any
internal staff alerts / outbound messages.

    python -m scripts.maitre_d_demo
"""

from __future__ import annotations

from datetime import datetime

from app.maitre_d.agent import MaitreD
from app.maitre_d.config import VenueConfig
from app.maitre_d.store import Store

# Fixed "now" so the demo is reproducible: Mon 15 Jun 2026, 11:00.
NOW = datetime(2026, 6, 15, 11, 0)


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
                 client=None, now_fn=lambda: NOW)

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
    print("Reservations on the book:")
    for r in md.store.list_reservations():
        print(f"  • {r.when:%a %d %b %I:%M%p}  party {r.party_size}  "
              f"table {r.table_id}  {r.status}  vip={r.is_vip}  risk={r.no_show_band}")


if __name__ == "__main__":
    main()
