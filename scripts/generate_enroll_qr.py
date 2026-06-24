#!/usr/bin/env python3
"""Generate counter enroll QR PNG — opens WhatsApp community chat."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qrcode


from app.community.menu_context import build_enroll_qr_url
from app.community.store import load_venue_config
from app.services.messaging import twilio_whatsapp_digits

OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    digits = twilio_whatsapp_digits("TWILIO_WHATSAPP_CUSTOMER_FROM")
    if not digits:
        print("Set TWILIO_WHATSAPP_CUSTOMER_FROM in .env")
        sys.exit(1)
    config = load_venue_config()
    url = build_enroll_qr_url(digits, config.qr_greeting)
    OUT.mkdir(exist_ok=True)
    out_path = OUT / "enroll_qr.png"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(out_path)
    print(config.venue_name)
    print(f"  WhatsApp: {url}")
    print(f"  QR PNG:   {out_path}")


if __name__ == "__main__":
    main()
