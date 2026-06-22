# 🛠️ tools — Developer Utilities

Cross-cutting helper scripts that don't belong to any single agent. Run them
from the repo root.

| Script                | What it does                                                    |
|-----------------------|-----------------------------------------------------------------|
| `sanity_check.py`     | Loads the dataset and prints summary stats (orders, revenue, voids, margins). A quick "is the data healthy?" smoke test. |
| `generate_holdout.py` | Regenerates `data/holdout/` — a second synthetic dataset with a *different* planted offender and seed, used by `tests/test_holdout.py` to prove the detection logic generalizes. |

```bash
python tools/sanity_check.py
python tools/generate_holdout.py
```
