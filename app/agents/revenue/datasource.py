"""Load POS data and slice it to a reporting period.

Thin wrapper over the existing ingest loader so the Revenue agent reads the same
canonical ``Order``/``MenuItem``/``Staff`` objects as every other tool.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from app.ingest.loader import load_dataset
from app.models.canonical import MenuItem, Order, Staff

# Default location of the synthetic café dataset shipped with the repo.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


def load_pos(data_dir: str | Path | None = None) -> tuple[list[Order], dict[str, MenuItem], dict[str, Staff]]:
    d = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    return load_dataset(d / "sales_detail.csv", d / "menu.csv", d / "staff.csv")


def normalize_period(period: str) -> str:
    p = (period or "week").strip().lower()
    aliases = {
        "today": "day", "daily": "day", "day": "day",
        "week": "week", "weekly": "week", "this week": "week", "7d": "week",
        "month": "month", "monthly": "month", "this month": "month", "30d": "month",
    }
    return aliases.get(p, "week")


def period_label(period: str, start: date, end: date) -> str:
    names = {"day": "day", "week": "week", "month": "month"}
    span = names.get(period, "period")
    if period == "day":
        return f"{end:%a %d %b}"
    return f"the last {PERIOD_DAYS.get(period, 7)} days ({start:%d %b} – {end:%d %b})"


def filter_period(
    orders: list[Order], period: str, as_of: date | None = None
) -> tuple[list[Order], date, date]:
    """Return (orders_in_window, start_date, end_date) for a trailing period.

    ``as_of`` defaults to the latest order date in the data so the agent works
    against historical exports as well as live data.
    """
    period = normalize_period(period)
    if not orders:
        today = as_of or date.today()
        return [], today, today
    end = as_of or max(o.datetime.date() for o in orders)
    days = PERIOD_DAYS.get(period, 7)
    start = end - timedelta(days=days - 1)
    window = [o for o in orders if start <= o.datetime.date() <= end]
    return window, start, end

