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


def load_pos_from_db(store_id: int) -> tuple[list[Order], dict[str, MenuItem], dict[str, Staff]]:
    """Load POS data from the uploaded_files DB table (same source as integrity agent)."""
    import csv, io, logging
    from app.core.db import SessionLocal, UploadedFile
    from app.ingest.loader import row_to_menu_item, row_to_staff, rows_to_orders
    from app.agents.integrity.pos.base import _load_mapping

    log = logging.getLogger(__name__)
    mapping = _load_mapping(None)

    with SessionLocal() as db:
        rows = db.query(UploadedFile).filter(UploadedFile.store_id == store_id).all()
        files: dict[str, str] = {r.file_type: r.content for r in rows}

    orders: list[Order] = []
    menu: dict[str, MenuItem] = {}
    staff: dict[str, Staff] = {}

    if "pos_sales" in files:
        reader = csv.DictReader(io.StringIO(files["pos_sales"]))
        orders = rows_to_orders(list(reader), mapping.SALES_DETAIL)
        log.info("revenue.db_load: store=%d orders=%d", store_id, len(orders))

    if "pos_menu" in files:
        reader = csv.DictReader(io.StringIO(files["pos_menu"]))
        items = [row_to_menu_item(row, mapping.MENU) for row in reader]
        menu = {item.sku: item for item in items}
        log.info("revenue.db_load: store=%d menu_items=%d", store_id, len(menu))

    if "pos_staff" in files:
        reader = csv.DictReader(io.StringIO(files["pos_staff"]))
        staff_list = [row_to_staff(row, mapping.STAFF) for row in reader]
        staff = {s.staff_id: s for s in staff_list}
        log.info("revenue.db_load: store=%d staff=%d", store_id, len(staff))

    return orders, menu, staff


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

