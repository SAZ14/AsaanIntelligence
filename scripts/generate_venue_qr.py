#!/usr/bin/env python3
"""Generate permanent venue QR code PNG for table placement."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qrcode

from app.api.deps import JOIN_BASE_URL, venues_path
from app.services.guest import load_venues

OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    venues = load_venues(venues_path())
    if not venues:
        print("No venues in data/venues.json")
        sys.exit(1)

    OUT.mkdir(exist_ok=True)
    for slug, venue in venues.items():
        url = f"{JOIN_BASE_URL.rstrip('/')}/join/{slug}"
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        out_path = OUT / f"venue_qr_{slug}.png"
        img.save(out_path)
        print(f"{venue.name}")
        print(f"  Join URL: {url}")
        print(f"  QR PNG:   {out_path}")
        print()


if __name__ == "__main__":
    main()
