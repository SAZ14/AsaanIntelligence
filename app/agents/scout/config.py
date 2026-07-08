import os
from dotenv import load_dotenv

load_dotenv()

# --- API keys ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() == "true"

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_IG_ACTOR = "apify/instagram-post-scraper"
APIFY_WEBSITE_ACTOR = "apify/website-content-crawler"
APIFY_SEARCH_ACTOR = "apify/google-search-scraper"
APIFY_MAPS_ACTOR = "compass/google-maps-reviews-scraper"
APIFY_REVIEWS_PER_COMPETITOR = 15   # reviews pulled per competitor via Google Maps
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.7")

# --- Pipeline tuning ---
FRESHNESS_MINUTES = 1440            # use cached run if last scout was < 24 hours ago
IG_POSTS_PER_PROFILE = 6           # posts scraped per Instagram profile
MAX_NEW_COMPETITORS = 10            # max auto-discovered competitors added per run

# A live run has been confirmed live to take anywhere from 7 to 45 minutes
# (many Apify actors fanned out per store). The "is a run already in
# flight for this store" guards (gateway/main.py x2, gateway/internal.py's
# _scout()) use this to decide whether a "running" row still represents
# real, ongoing work or a crashed/orphaned process that should no longer
# block a fresh attempt. Must stay comfortably above the observed max
# duration -- a cutoff shorter than that (previously 15 min, confirmed too
# short) stops recognizing a genuinely still-running scrape as in flight
# partway through, letting a second trigger start a duplicate concurrent
# scrape for the same store and waste Apify credits.
RUN_IN_FLIGHT_MINUTES = 60

# --- Cost/relevance ceilings ---
# Without these, a live run's cost scales unbounded with however many
# competitors have accumulated (discovery only ever adds, never removes) and
# however many raw items a scraper happens to return for one competitor
# (a multi-branch chain like KFC can return 200+ Google Maps reviews despite
# a per-competitor request of 15 -- confirmed live).
MAX_SCRAPED_COMPETITORS = 12        # hard ceiling on competitors scraped per live run;
                                     # primary/seed always kept, discovered ones ranked
                                     # by historical finding count when over the cap.
                                     # ~3 Apify calls/competitor (1 web crawl + 2 review
                                     # passes; Instagram is batched across all of them),
                                     # so this is the real per-run cost lever.
MAX_FINDINGS_PER_COMPETITOR = 12    # per competitor, after dedup, before enrichment;
                                     # balanced across strength/weakness/trend rather
                                     # than truncating to whichever bucket scraped first
PRUNE_MIN_AGE_DAYS = 3              # only prune "discovered" competitors past this age
                                     # (give them a few live runs to prove themselves)
PRUNE_MIN_FINDINGS = 1              # a discovered competitor needs at least this many
                                     # Finding rows ever, or it's removed as noise

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


def enabled_sources() -> dict:
    return {
        "web": bool(APIFY_TOKEN),            # website crawl + web search via Apify
        "instagram": bool(APIFY_TOKEN),
        "google_reviews": bool(APIFY_TOKEN), # deep Google Maps reviews via Apify
        "zai": bool(ZAI_API_KEY),
        "twilio": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
    }
