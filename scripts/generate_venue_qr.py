#!/usr/bin/env python3
"""Generate permanent venue QR code PNG — opens WhatsApp chat with the Customer Agent."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qrcode

from app.services.guest import load_venues
from app.services.messaging import twilio_whatsapp_digits
from app.services.whatsapp_agent import build_whatsapp_qr_url

OUT = Path(__file__).resolve().parent.parent / "output"


def venues_path() -> Path:
    data = os.environ.get(
        "ASAAN_DATA_DIR",
        Path(__file__).resolve().parent.parent / "data",
    )
    return Path(data) / "venues.json"


def main() -> None:
    wa_digits = twilio_whatsapp_digits()
    if not wa_digits:
        print("Set TWILIO_WHATSAPP_FROM in .env (e.g. whatsapp:+14155238886)")
        sys.exit(1)

    venues = load_venues(venues_path())
    if not venues:
        print("No venues in data/venues.json")
        sys.exit(1)

    OUT.mkdir(exist_ok=True)
    for slug, venue in venues.items():
        greeting = venue.whatsapp_greeting or f"Hi {venue.name}! I'd like to join rewards."
        url = build_whatsapp_qr_url(f"+{wa_digits}", venue.name, greeting=greeting)
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        out_path = OUT / f"venue_qr_{slug}.png"
        img.save(out_path)
        print(f"{venue.name}")
        print(f"  WhatsApp: {url}")
        print(f"  QR PNG:   {out_path}")
        print()


if __name__ == "__main__":
    main()
