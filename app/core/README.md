# 🧱 core — Shared Foundation

**Not an agent.** This is the common ground every agent stands on. If you change
something here, you affect *all four agents*, so tread carefully.

## What's inside

| Path                       | What it is                                                   |
|----------------------------|--------------------------------------------------------------|
| `models.py`                | The canonical data types: `Order`, `LineItem`, `Payment`, `MenuItem`, `Staff`, `Review`, `Venue`. Built with Pydantic. |
| `ingest/loader.py`         | Reads the CSVs and builds the models above. Entry point: `load_dataset()`. |
| `ingest/mappings/`         | One file per POS format, mapping that POS's column names to our canonical fields. |

## Supporting a new POS export

Real POS systems all name their CSV columns differently. You **don't** edit the
loader — you add a mapping:

1. Copy `ingest/mappings/cafe_generic.py` to a new file.
2. Change the right-hand-side column names to match the new POS export.
3. Pass your mapping module to `load_dataset(..., mapping_module=your_mapping)`.

No agent code changes — every agent speaks the canonical models, not raw CSV.
