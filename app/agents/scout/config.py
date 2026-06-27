import os
from dotenv import load_dotenv

load_dotenv()

# --- API keys ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() == "true"

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_IG_ACTOR = os.getenv("APIFY_IG_ACTOR", "apify/instagram-post-scraper")
APIFY_WEBSITE_ACTOR = os.getenv("APIFY_WEBSITE_ACTOR", "apify/website-content-crawler")
APIFY_SEARCH_ACTOR = os.getenv("APIFY_SEARCH_ACTOR", "apify/google-search-scraper")
APIFY_MAPS_ACTOR = os.getenv("APIFY_MAPS_ACTOR", "apify/google-maps-reviews-scraper")
APIFY_REVIEWS_PER_COMPETITOR = int(os.getenv("APIFY_REVIEWS_PER_COMPETITOR", "20"))
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.7")

# --- Pipeline tuning ---
FRESHNESS_MINUTES = int(os.getenv("FRESHNESS_MINUTES", "90"))
IG_POSTS_PER_PROFILE = int(os.getenv("IG_POSTS_PER_PROFILE", "8"))
MAX_NEW_COMPETITORS = int(os.getenv("MAX_NEW_COMPETITORS", "3"))

# --- Competitor seed list ---
COMPETITORS = [
    {
        "name": "Baskin Robbins Pakistan",
        "category": "Ice cream",
        "instagram_handle": "baskinrobbinspk",
        "website": "https://baskinrobbins.pk",
        "notes": "Primary ice-cream competitor; branches incl. I-8 Islamabad",
    },
    {
        "name": "Layers",
        "category": "Bakeshop / cakes",
        "instagram_handle": "layers.bakeshop",
        "website": None,
        "notes": "Large following, cake-led content",
    },
    {
        "name": "Tehzeeb Bakers",
        "category": "Bakery",
        "instagram_handle": "tehzeeb.pk",
        "website": "https://tehzeeb.com",
        "notes": "G-9 + multiple branches",
    },
    {
        "name": "Loafology Bakery & Cafe",
        "category": "Bakery / cafe",
        "instagram_handle": None,
        "website": None,
        "notes": "Jinnah Ave, Blue Area; F-11 branch — handle to be resolved",
    },
    {
        "name": "Burning Brownie",
        "category": "Dessert cafe",
        "instagram_handle": None,
        "website": None,
        "notes": "Beverly Centre, F-6/1 Blue Area; cheesecakes/brownies — handle to be resolved",
    },
    {
        "name": "Kitchen Cuisine",
        "category": "Bakery",
        "instagram_handle": None,
        "website": None,
        "notes": "F-10; Ferrero Rocher cake, cheesecakes — handle to be resolved",
    },
    {
        "name": "O'Brownies",
        "category": "Brownies / dessert",
        "instagram_handle": None,
        "website": None,
        "notes": "Brownie-led — handle to be resolved",
    },
]

# Sugar Rush itself — baseline, not a competitor
SUGAR_RUSH = {
    "name": "Sugar Rush",
    "instagram_handle": "sugarrushisb",
    "location": "Kohsar Market, F-6, Islamabad",
}


def enabled_sources() -> dict:
    return {
        "web": bool(APIFY_TOKEN),            # website crawl + web search via Apify
        "instagram": bool(APIFY_TOKEN),
        "google_reviews": bool(APIFY_TOKEN), # deep Google Maps reviews via Apify
        "zai": bool(ZAI_API_KEY),
        "twilio": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
    }
