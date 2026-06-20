#!/usr/bin/env python3
"""Generate a printable QR poster that encodes the venue's wa.me loyalty link.

Scanning the QR opens WhatsApp to the business with the stamp message ready to
send; sending it registers the customer's first scan.

    python scripts/generate_qr.py --number +14155238886 --out venue_qr.png

Requires the optional 'qr' extra:  pip install -e .[qr]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import DEFAULT_PREFILL, build_wa_link


def _poster(qr_img, venue: str, reward_line: str):
    """Place the QR on a white card with a heading + call to action."""
    from PIL import Image, ImageDraw

    qr_img = qr_img.convert("RGB")
    qw, qh = qr_img.size
    margin, top, bottom = 60, 130, 120
    canvas = Image.new("RGB", (qw + margin * 2, qh + top + bottom), "white")
    canvas.paste(qr_img, (margin, top))

    draw = ImageDraw.Draw(canvas)

    def centered(text, y, size_hint):
        try:
            w = draw.textlength(text, font_size=size_hint)  # Pillow >= 10
            draw.text(((canvas.width - w) / 2, y), text, fill="black", font_size=size_hint)
        except TypeError:  # older Pillow: default bitmap font, no sizing
            w = draw.textlength(text)
            draw.text(((canvas.width - w) / 2, y), text, fill="black")

    centered(f"{venue} Rewards", 38, 46)
    centered("Scan with your camera to collect stamps", top + qh + 24, 26)
    centered(reward_line, top + qh + 60, 26)
    return canvas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--number", required=True, help="Business WhatsApp number, e.g. +14155238886")
    ap.add_argument("--text", default=DEFAULT_PREFILL, help="Pre-filled WhatsApp message")
    ap.add_argument("--out", default="venue_qr.png", help="Output PNG path")
    ap.add_argument("--venue", default="Sugar Rush")
    ap.add_argument("--reward", default="5 stamps = a free ice cream", help="Call-to-action line")
    ap.add_argument("--plain", action="store_true", help="Bare QR, no poster framing")
    args = ap.parse_args()

    link = build_wa_link(args.number, args.text)
    try:
        import qrcode
    except ImportError:
        sys.exit("qrcode not installed. Run: pip install -e .[qr]  (or: pip install 'qrcode[pil]')")

    qr = qrcode.QRCode(
        box_size=10, border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    if not args.plain:
        try:
            img = _poster(img, args.venue, args.reward)
        except Exception as exc:  # never fail just because of poster framing
            print(f"(poster framing skipped: {exc})")

    out = Path(args.out)
    img.save(out)
    print(f"Link:  {link}")
    print(f"Saved: {out.resolve()}")


if __name__ == "__main__":
    main()
