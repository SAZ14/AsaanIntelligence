# Competitive intelligence fixtures

Synthetic snapshots used by the Competitive Intelligence agent when no live
Browserbase/Supabase credentials are configured. They make the full pipeline
runnable and testable offline.

- `snapshot_prev.json` — a baseline capture (~30 days before current).
- `snapshot_current.json` — the latest capture.

Diffing the two is what surfaces **new dishes** and **review momentum**; a
brand-new venue (Crust & Co, F-7) appears only in the current file with no
baseline, exercising new-venue detection.

## Shape

Each file is `{ "competitors": [ <CompetitorSnapshot>, ... ] }`, where a
snapshot matches `app.models.competitive.CompetitorSnapshot`:

```json
{
  "competitor": {"competitor_id": "...", "name": "...", "area": "F-7",
                 "city": "Islamabad", "country": "Pakistan", "opened_at": "..."},
  "captured_at": "2026-06-16T09:00:00",
  "menu": [{"name": "...", "category": "...", "price": 0, "tags": []}],
  "promotions": [{"title": "...", "description": "...", "discount_pct": 50}],
  "reviews": [{"source": "Google", "rating_avg": 4.6, "review_count": 820}]
}
```

## Going live

Set credentials and the factories switch automatically — no code change:

| Service     | Env vars                                            | Install                  |
|-------------|-----------------------------------------------------|--------------------------|
| Browserbase | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`     | `pip install -e .[scrape]`  |
| Supabase    | `SUPABASE_URL`, `SUPABASE_KEY`                       | `pip install -e .[storage]` |
| Claude      | `ANTHROPIC_API_KEY`                                 | (already a core dep)     |

See `app/storage/supabase.py` for the suggested `competitor_snapshots` table.

Run it: `python scripts/competitive_report.py`
