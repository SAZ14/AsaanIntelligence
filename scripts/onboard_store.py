"""
Generic store onboarding script.

Usage (coding agent):
  1. Fill in every section below marked with  ← FILL IN
  2. Run:  python scripts/onboard_store.py
  3. Script is fully idempotent — safe to re-run after corrections.

What this script does:
  • Creates / updates the store record
  • Registers the WhatsApp number
  • Adds all staff members (owners, managers, staff)
  • Sets up reputation scraping config (Google Maps, FoodPanda, Instagram)
  • Sets brand voice for AI-drafted review replies
  • Seeds competitors across local / national / trend tiers
  • Optionally configures the customer loyalty app
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")


# ══════════════════════════════════════════════════════════════════════════════
# STORE IDENTITY                                                    ← FILL IN
# ══════════════════════════════════════════════════════════════════════════════

STORE = {
    "name":               "Restaurant Name",          # ← display name
    "location":           "Area, Sector, City",       # ← human-readable address
    "category":           "burgers",                  # ← food category (burgers / pizza / desserts / etc.)
    "instagram_handle":   "instagramhandle",          # ← without @ sign; None if not on Instagram
}


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP NUMBER                                                   ← FILL IN
# ══════════════════════════════════════════════════════════════════════════════
# The number Twilio will receive messages on for this restaurant.
# Format: "whatsapp:+<country-code><number>"  e.g. "whatsapp:+923001234567"
# Set to None to skip (you can register it later via POST /admin/stores/{id}/twilio)

WHATSAPP_NUMBER = None   # ← e.g. "whatsapp:+923001234567"


# ══════════════════════════════════════════════════════════════════════════════
# STAFF MEMBERS                                                     ← FILL IN
# ══════════════════════════════════════════════════════════════════════════════
# role options: "owner" | "manager" | "staff"
# phone format: "whatsapp:+<country-code><number>"

MEMBERS = [
    # {"phone": "whatsapp:+923001234567", "role": "owner"},
    # {"phone": "whatsapp:+923009876543", "role": "manager"},
]


# ══════════════════════════════════════════════════════════════════════════════
# REPUTATION / REVIEW SCRAPING                                      ← FILL IN
# ══════════════════════════════════════════════════════════════════════════════

REPUTATION = {
    # All name variants to search on Google Maps (catches all branches)
    "google_maps_terms": [
        # "Restaurant Name",
        # "Restaurant Name City",
        # "Restaurant Name Branch Area",
    ],
    "google_maps_location": "City, Country",          # ← e.g. "Islamabad, Pakistan"

    # FoodPanda listing — set both to None if not on FoodPanda
    "foodpanda_url":     None,   # ← e.g. "https://www.foodpanda.pk/restaurant/xxxx/name"
    "foodpanda_keyword": None,   # ← e.g. "Restaurant Name"

    # Your own Instagram handles for scraping your own posts + comments
    "instagram_usernames": [
        # "instagramhandle",
    ],

    # Brand voice — used by AI to draft review replies in your style
    # Be specific: personality, tone, things that reflect the brand concept
    "brand_voice_tone": (
        "Professional, warm, and grateful. Address issues directly and "
        "invite the customer back. Keep replies concise."
    ),
    # Words / phrases the AI must never use in replies
    "brand_voice_never_say": [
        "unfortunately",
        "we apologize",
        "we regret",
        "I'm sorry",
        "apologies",
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# COMPETITORS                                                       ← FILL IN
# ══════════════════════════════════════════════════════════════════════════════
# Organised in 3 tiers. Each entry needs at minimum a "name".
# "instagram_handle": None  →  the discovery engine will search for it automatically.
# For hashtag trend tracking use "#hashtag" as the instagram_handle.

# Tier 1 — Direct local competitors (same city, same category)
LOCAL: list[dict] = [
    # {
    #     "name": "Competitor Name",
    #     "category": "burgers",
    #     "instagram_handle": "theirhandle",   # or None to auto-discover
    #     "notes": "Brief note — location, positioning, why they matter",
    # },
]

# Tier 2 — National players to benchmark against (different city, same category)
NATIONAL: list[dict] = [
    # {
    #     "name": "National Chain",
    #     "category": "burgers",
    #     "instagram_handle": "theirhandle",
    #     "notes": "Lahore-based; track content strategy and pricing",
    # },
]

# Tier 3 — Global trend hashtags (not competitors, but trend signals)
# instagram_handle must start with "#"
TRENDS: list[dict] = [
    # {
    #     "name": "Global Trend: Smash Burgers",
    #     "category": "international_trend",
    #     "instagram_handle": "#smashburger",
    #     "notes": "Global smash burger content — techniques and formats going viral",
    # },
]


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMER LOYALTY APP  (optional)                                  ← FILL IN
# ══════════════════════════════════════════════════════════════════════════════
# Leave as None to skip — configure later via POST /admin/stores/{id}/customer

CUSTOMER_CONFIG = None

# To enable, replace None with a dict:
# CUSTOMER_CONFIG = {
#     "venue_name":        "Restaurant Name",
#     "stamp_goal":        10,          # stamps needed for a free reward
#     "reward_description": "1 Free Burger",
#     "welcome_message":  "Welcome to Restaurant Name loyalty! 🎉",
# }


# ══════════════════════════════════════════════════════════════════════════════
# ONBOARDING LOGIC  (do not edit below this line)
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    from app.core.db import (
        SessionLocal, Store, StoreTwilioNumber, StoreMember,
        ReputationConfig, VenueConfig, init_db,
    )
    from app.agents.scout.discovery import seed_competitors_for_store

    init_db()

    with SessionLocal() as db:

        # ── 1. Store record ───────────────────────────────────────────────────
        store = db.query(Store).filter(Store.name == STORE["name"]).first()
        if store is None:
            store = Store(
                name=STORE["name"],
                location=STORE["location"],
                category=STORE["category"],
                instagram_handle=STORE["instagram_handle"],
            )
            db.add(store)
            db.commit()
            db.refresh(store)
            print(f"[+] Store created: {store.name} (id={store.id})")
        else:
            store.location = STORE["location"]
            store.category = STORE["category"]
            store.instagram_handle = STORE["instagram_handle"]
            db.commit()
            print(f"[~] Store already exists: {store.name} (id={store.id}) — fields updated")

        store_id = store.id

        # ── 2. WhatsApp number ────────────────────────────────────────────────
        if WHATSAPP_NUMBER:
            existing_num = db.query(StoreTwilioNumber).filter(
                StoreTwilioNumber.store_id == store_id
            ).first()
            if existing_num is None:
                db.add(StoreTwilioNumber(store_id=store_id, whatsapp_number=WHATSAPP_NUMBER))
                db.commit()
                print(f"[+] WhatsApp number registered: {WHATSAPP_NUMBER}")
            elif existing_num.whatsapp_number != WHATSAPP_NUMBER:
                existing_num.whatsapp_number = WHATSAPP_NUMBER
                db.commit()
                print(f"[~] WhatsApp number updated: {WHATSAPP_NUMBER}")
            else:
                print(f"[=] WhatsApp number already set: {WHATSAPP_NUMBER}")
        else:
            print("[!] WhatsApp number skipped — set WHATSAPP_NUMBER or register via API later")

        # ── 3. Staff members ──────────────────────────────────────────────────
        if MEMBERS:
            existing_phones = {
                m.phone
                for m in db.query(StoreMember).filter(StoreMember.store_id == store_id).all()
            }
            added_members = 0
            for m in MEMBERS:
                phone = m["phone"]
                if phone not in existing_phones:
                    db.add(StoreMember(store_id=store_id, phone=phone, role=m.get("role", "staff")))
                    existing_phones.add(phone)
                    added_members += 1
            if added_members:
                db.commit()
            print(f"[+] Members: {added_members} added, {len(MEMBERS) - added_members} already existed")
        else:
            print("[!] No members defined — add via POST /admin/stores/{id}/members")

        # ── 4. Reputation config ──────────────────────────────────────────────
        rc = db.query(ReputationConfig).filter(ReputationConfig.store_id == store_id).first()
        if rc is None:
            rc = ReputationConfig(store_id=store_id)
            db.add(rc)
        rc.google_maps_terms      = REPUTATION["google_maps_terms"]
        rc.google_maps_location   = REPUTATION["google_maps_location"]
        rc.foodpanda_url          = REPUTATION["foodpanda_url"]
        rc.foodpanda_keyword      = REPUTATION["foodpanda_keyword"]
        rc.instagram_usernames    = REPUTATION["instagram_usernames"]
        rc.brand_voice_tone       = REPUTATION["brand_voice_tone"]
        rc.brand_voice_never_say  = REPUTATION["brand_voice_never_say"]
        db.commit()
        print("[+] Reputation config set")

        # ── 5. Customer loyalty config ────────────────────────────────────────
        if CUSTOMER_CONFIG:
            vc = db.query(VenueConfig).filter(VenueConfig.store_id == store_id).first()
            if vc is None:
                vc = VenueConfig(store_id=store_id)
                db.add(vc)
            for key, val in CUSTOMER_CONFIG.items():
                setattr(vc, key, val)
            db.commit()
            print("[+] Customer loyalty config set")
        else:
            print("[!] Customer loyalty config skipped — configure later via POST /admin/stores/{id}/customer")

    # ── 6. Competitors ────────────────────────────────────────────────────────
    all_competitors = LOCAL + NATIONAL + TRENDS
    if all_competitors:
        added = seed_competitors_for_store(store_id, competitors=all_competitors)
        skipped = len(all_competitors) - added
        print(
            f"[+] Competitors: {added} seeded, {skipped} already existed"
            f"  ({len(LOCAL)} local / {len(NATIONAL)} national / {len(TRENDS)} trend hashtags)"
        )
    else:
        print("[!] No competitors defined — seed later via POST /admin/stores/{id}/seed-competitors")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\nOnboarding complete. Store ID = {store_id}")
    print("\nNext steps:")
    if not WHATSAPP_NUMBER:
        print(f"  Register Twilio number  ->  POST /admin/stores/{store_id}/twilio")
    if not MEMBERS:
        print(f"  Add staff members       ->  POST /admin/stores/{store_id}/members")
    if not CUSTOMER_CONFIG:
        print(f"  Set loyalty config      ->  POST /admin/stores/{store_id}/customer")
    print(f"  Set Twilio webhook      ->  https://asaanintelligence.up.railway.app/whatsapp")
    print(f"  Resolve missing handles ->  GET  /admin/stores/{store_id}/seed-competitors")


if __name__ == "__main__":
    main()
