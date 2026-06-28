"""One-time onboarding script for Anatummy restaurant.

Run with:
    python scripts/onboard_anatummy.py

Idempotent — safe to run multiple times.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")


# ── Anatummy store identity ────────────────────────────────────────────────────

STORE = {
    "name": "Anatummy",
    "location": "Beverly Centre, F-6/1, Islamabad",
    "category": "burgers",
    "instagram_handle": "anatummyisb",
}

# ── Reputation / review scraping config ───────────────────────────────────────

REPUTATION = {
    # All known Google Maps branch names — scraper fetches reviews for each
    "google_maps_terms": [
        "Anatummy",
        "Anatummy Islamabad",
        "Anatummy Beverly",
        "Anatummy F8",
        "Anatummy Bahria",
    ],
    "google_maps_location": "Islamabad, Pakistan",

    # Primary FoodPanda listing (main branch)
    "foodpanda_url": "https://www.foodpanda.pk/restaurant/e4hp/anatummy",
    "foodpanda_keyword": "Anatummy",

    # Own Instagram handle for own-venue posts + comments
    "instagram_usernames": ["anatummyisb"],

    # Brand voice — "burgers with a touch of doctor" concept
    "brand_voice_tone": (
        "Bold, witty, and medically themed. Confident and fun. "
        "Reference the doctor concept when it fits. Thank by name, "
        "address issues with humour and professionalism."
    ),
    "brand_voice_never_say": [
        "unfortunately",
        "we apologize",
        "we regret",
        "I'm sorry",
        "apologies",
    ],
}

# ── Competitor lists ───────────────────────────────────────────────────────────

# Direct competitors in Islamabad — scout tracks Instagram + Google Reviews
LOCAL_ISLAMABAD: list[dict] = [
    {
        "name": "Meg",
        "category": "burgers",
        "instagram_handle": "megpakistan",
        "notes": "Premium smash burgers; expanding nationally from Lahore into Islamabad",
    },
    {
        "name": "TBC (The Burger Co.)",
        "category": "burgers",
        "instagram_handle": "theburgerco.pk",
        "notes": "Beverly Centre F-6 & F-11 Markaz — direct neighbour/overlap",
    },
    {
        "name": "Nosh",
        "category": "burgers",
        "instagram_handle": "nosh_burger",
        "notes": "G-9/4 Taqwa Market; beef burgers; local following",
    },
    {
        "name": "Burger Lab",
        "category": "burgers",
        "instagram_handle": "burgerlabpk",
        "notes": "145K followers; smash burgers since 2014; Bahria Enclave branch",
    },
    {
        "name": "OPTP",
        "category": "fast food / fries",
        "instagram_handle": "optpfries",
        "notes": "354K followers; fries + chicken + burgers; strong promo game",
    },
    {
        "name": "Binge",
        "category": "burgers",
        "instagram_handle": "binge_pk",
        "notes": "F-10/2 Tariq Market; affordable beef burgers; loyal local fanbase",
    },
    {
        "name": "Buns",
        "category": "burgers",
        "instagram_handle": None,  # to be resolved by discovery engine
        "notes": "Islamabad — handle TBD via auto-discovery",
    },
    {
        "name": "Bangin Buns",
        "category": "burgers",
        "instagram_handle": "banginbuns.pk",
        "notes": "I-8 Markaz; smash burger specialist; 7K followers, growing fast",
    },
    {
        "name": "Brim",
        "category": "burgers",
        "instagram_handle": "brimburgerspk",
        "notes": "Multi-city chain; beef burgers; active content",
    },
    {
        "name": "Daily Deli",
        "category": "burgers / deli",
        "instagram_handle": "thedailydeli",
        "notes": "4.8-rated designer burgers; Islamabad + Lahore",
    },
    {
        "name": "KFC",
        "category": "fast food",
        "instagram_handle": None,  # global chain — discovery will resolve PK handle
        "notes": "International chain; crispy chicken + burgers; benchmark for value perception",
    },
    {
        "name": "McDonald's",
        "category": "fast food",
        "instagram_handle": None,  # global chain — discovery will resolve PK handle
        "notes": "International chain; benchmark for promotions, pricing, and family deals",
    },
]

# National heavy-hitters — scout tracks for content strategy and trend signals
NATIONAL: list[dict] = [
    {
        "name": "Smash",
        "category": "smash burgers",
        "instagram_handle": "smashlahore",
        "notes": "60K followers; Lahore + Gujranwala; premium smash concept to benchmark",
    },
    {
        "name": "Johnny & Jugnu",
        "category": "burgers / wraps",
        "instagram_handle": "itsmecalu",
        "notes": "51K followers; Lahore chain; strong brand identity and wrap/burger combo",
    },
    {
        "name": "Salt",
        "category": "premium burgers",
        "instagram_handle": "salt_pakistan",
        "notes": "Wagyu beef; Lahore multi-branch; high-end benchmark for quality positioning",
    },
    {
        "name": "Spread Lahore",
        "category": "burgers",
        "instagram_handle": "spreadlahore",
        "notes": "30K followers; Lahore; daily 1PM–12AM; track content cadence",
    },
    {
        "name": "Cravy Lahore",
        "category": "burgers",
        "instagram_handle": "cravv.pk",
        "notes": "31K followers; Model Town & DHA Phase 5; takeaway/delivery focus",
    },
    {
        "name": "Tot Lahore",
        "category": "premium fast food",
        "instagram_handle": "totpakistan",
        "notes": "By food vlogger RHS; Model Town Lahore; wagyu-style; heavy food-creator audience",
    },
]

# Global trend tracking — the Instagram scraper pulls top posts for each hashtag
# These are NOT competitors but trend signals to inform menu/content decisions
INTERNATIONAL_TRENDS: list[dict] = [
    {
        "name": "Global Trend: Burgers",
        "category": "international_trend",
        "instagram_handle": "#burger",
        "notes": "Top trending burger content globally — new styles, presentations, formats",
    },
    {
        "name": "Global Trend: Smash Burgers",
        "category": "international_trend",
        "instagram_handle": "#smashburger",
        "notes": "Global smash burger boom — techniques, toppings, concepts going viral",
    },
    {
        "name": "Global Trend: Crispy Chicken",
        "category": "international_trend",
        "instagram_handle": "#crispychicken",
        "notes": "Crispy chicken sandwich trend — what styles/sauces are popping globally",
    },
    {
        "name": "Global Trend: Loaded Fries",
        "category": "international_trend",
        "instagram_handle": "#loadedfries",
        "notes": "Loaded fries innovation globally — toppings, formats, pricing signals",
    },
    {
        "name": "Global Trend: Fast Food",
        "category": "international_trend",
        "instagram_handle": "#fastfood",
        "notes": "Broad fast food trends — promos, collabs, limited editions gaining traction",
    },
]

ALL_COMPETITORS = LOCAL_ISLAMABAD + NATIONAL + INTERNATIONAL_TRENDS


# ── Onboarding logic ───────────────────────────────────────────────────────────

def main() -> None:
    from app.core.db import SessionLocal, Store, ReputationConfig, init_db
    from app.agents.scout.discovery import seed_competitors_for_store

    init_db()

    with SessionLocal() as db:
        # 1. Find or create store
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
            print(f"Created store: {store.name} (id={store.id})")
        else:
            # Update fields in case they changed
            store.location = STORE["location"]
            store.category = STORE["category"]
            store.instagram_handle = STORE["instagram_handle"]
            db.commit()
            print(f"Store already exists: {store.name} (id={store.id}) — updated fields")

        store_id = store.id

        # 2. Reputation / review config
        rc = db.query(ReputationConfig).filter(ReputationConfig.store_id == store_id).first()
        if rc is None:
            rc = ReputationConfig(store_id=store_id)
            db.add(rc)
        rc.google_maps_terms = REPUTATION["google_maps_terms"]
        rc.google_maps_location = REPUTATION["google_maps_location"]
        rc.foodpanda_url = REPUTATION["foodpanda_url"]
        rc.foodpanda_keyword = REPUTATION["foodpanda_keyword"]
        rc.instagram_usernames = REPUTATION["instagram_usernames"]
        rc.brand_voice_tone = REPUTATION["brand_voice_tone"]
        rc.brand_voice_never_say = REPUTATION["brand_voice_never_say"]
        db.commit()
        print("Reputation config set (Google Maps, FoodPanda, Instagram, brand voice)")

    # 3. Seed all competitors
    added = seed_competitors_for_store(store_id, competitors=ALL_COMPETITORS)
    total = len(ALL_COMPETITORS)
    skipped = total - added
    print(
        f"Competitors: {added} seeded, {skipped} already existed  "
        f"({len(LOCAL_ISLAMABAD)} local · {len(NATIONAL)} national · "
        f"{len(INTERNATIONAL_TRENDS)} trend hashtags)"
    )

    print(f"\nAnatummy onboarded. Store ID = {store_id}")
    print("\nRemaining steps (via admin API or WhatsApp):")
    print(f"  Register Twilio number  ->  POST /admin/stores/{store_id}/twilio")
    print(f"  Add owner to WhatsApp   ->  POST /admin/stores/{store_id}/members")
    print(f"  Set loyalty config      ->  POST /admin/stores/{store_id}/customer")
    print(f"  Resolve missing handles ->  POST /admin/stores/{store_id}/seed-competitors")


if __name__ == "__main__":
    main()
