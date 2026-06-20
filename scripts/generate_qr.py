#!/usr/bin/env python3
"""Generate printable QR posters that encode each venue's wa.me loyalty link.

Scanning a QR opens WhatsApp to that restaurant with the stamp message ready to
send; sending it registers the customer's first scan there.

    # all restaurants from the config (one poster each):
    python scripts/generate_qr.py --config data/restaurants.example.json --all

    # one restaurant from the config:
    python scripts/generate_qr.py --config data/restaurants.example.json --restaurant burger_lab

    # ad-hoc, no config:
    python scripts/generate_qr.py --number +14155238886 --venue "Sugar Rush"

Requires the optional 'qr' extra:  pip install -e .[qr]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import DEFAULT_PREFILL, build_wa_link, load_registry


def _poster(qr_img, heading: str, cta1: str, cta2: str):
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

    centered(heading, 38, 46)
    centered(cta1, top + qh + 24, 26)
    centered(cta2, top + qh + 60, 26)
    return canvas


def make_poster(number: str, venue: str, reward_line: str, out: Path,
                text: str = DEFAULT_PREFILL, plain: bool = False) -> str:
    import qrcode

    link = build_wa_link(number, text)
    qr = qrcode.QRCode(box_size=10, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    if not plain:
        try:
            img = _poster(img, f"{venue} Rewards", "Scan with your camera to collect stamps", reward_line)
        except Exception as exc:
            print(f"(poster framing skipped: {exc})")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return link


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="restaurants JSON config")
    ap.add_argument("--restaurant", help="restaurant id from the config")
    ap.add_argument("--all", action="store_true", help="one poster per restaurant in the config")
    ap.add_argument("--number", help="ad-hoc business WhatsApp number (no config)")
    ap.add_argument("--venue", default="Sugar Rush")
    ap.add_argument("--reward", default="5 stamps = a free treat")
    ap.add_argument("--text", default=DEFAULT_PREFILL)
    ap.add_argument("--out", default="venue_qr.png", help="output PNG (single mode)")
    ap.add_argument("--out-dir", default="qr_posters", help="output dir (--all mode)")
    ap.add_argument("--plain", action="store_true", help="bare QR, no poster framing")
    args = ap.parse_args()

    try:
        import qrcode  # noqa: F401  (fail fast with a clear hint)
    except ImportError:
        sys.exit("qrcode not installed. Run: pip install -e .[qr]  (or: pip install 'qrcode[pil]')")

    if args.config:
        registry = load_registry(args.config, store_dir="loyalty_data")
        targets = registry.all() if args.all else (
            [registry.by_id(args.restaurant)] if args.restaurant else registry.all()
        )
        if args.restaurant and targets == [None]:
            sys.exit(f"No restaurant '{args.restaurant}' in {args.config}")
        out_dir = Path(args.out_dir)
        for r in targets:
            out = out_dir / f"{r.id}.png"
            link = make_poster(r.whatsapp_number, r.name, r.reward_line(), out, args.text, args.plain)
            print(f"{r.id:14s} {out}  ←  {link}")
        return

    if not args.number:
        sys.exit("Provide --config (with --all/--restaurant) or --number for ad-hoc mode.")
    out = Path(args.out)
    link = make_poster(args.number, args.venue, args.reward, out, args.text, args.plain)
    print(f"Link:  {link}\nSaved: {out.resolve()}")


if __name__ == "__main__":
    main()
