from __future__ import annotations

import json
from pathlib import Path

from app.database import supabase


def main() -> None:
    print("Setting up Supabase database...")

    seed_venue_config()
    seed_deals()
    print("Done.")


def seed_venue_config() -> None:
    existing = supabase.table("venue_config").select("id").limit(1).execute()
    if existing.data:
        print("venue_config already seeded, skipping.")
        return

    path = Path("data/venue_config.json")
    if not path.exists():
        print("data/venue_config.json not found, using defaults.")
        data = {}
    else:
        with open(path) as f:
            data = json.load(f)

    supabase.table("venue_config").insert({
        "id": 1,
        "venue_name": data.get("venue_name", "Sugar Rush"),
        "stamp_goal": data.get("stamp_goal", 5),
        "reward_text": data.get("reward_text", "a free drink or dessert"),
        "winback_days": data.get("winback_days", 5),
        "code_expiry_days": data.get("code_expiry_days", 30),
        "owner_phones": data.get("owner_phones", []),
        "qr_greeting": data.get("qr_greeting", "Hi Sugar Rush! I'd like to join the community."),
    }).execute()
    print("Seeded venue_config.")


def seed_deals() -> None:
    existing = supabase.table("deals").select("id").limit(1).execute()
    if existing.data:
        print("deals already seeded, skipping.")
        return

    path = Path("data/deals.json")
    if not path.exists():
        print("data/deals.json not found, skipping deals.")
        return

    with open(path) as f:
        deals = json.load(f)

    for d in deals:
        supabase.table("deals").insert({
            "title": d["title"],
            "description": d["description"],
            "active": d.get("active", True),
        }).execute()
    print(f"Seeded {len(deals)} deals.")


if __name__ == "__main__":
    main()
