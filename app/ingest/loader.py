from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime as dt
from pathlib import Path
from typing import Any

from app.models.canonical import LineItem, MenuItem, Order, Payment, Review, Staff
from app.ingest.mappings import cafe_generic as default_mapping


def _get(row: dict[str, str], mapping: dict[str, str], key: str) -> str:
    return str(row.get(mapping[key], "") or "").strip()


def _bool(val: str) -> bool:
    return val.strip() in ("1", "True", "true", "yes")


# ── Row-level normalisers ──
#
# These turn a single already-parsed record (a dict keyed by the source POS's
# field names) into a canonical model. File loaders below read rows off CSV; the
# REST POS connector feeds JSON records through the same functions, so every POS
# normalises identically regardless of transport.

def row_to_menu_item(row: dict, mapping: dict[str, str]) -> MenuItem:
    cost_raw = _get(row, mapping, "cost")
    return MenuItem(
        sku=_get(row, mapping, "sku"),
        name=_get(row, mapping, "name"),
        category=_get(row, mapping, "category"),
        cost=float(cost_raw) if cost_raw else None,
        price=float(_get(row, mapping, "price")),
    )


def row_to_staff(row: dict, mapping: dict[str, str]) -> Staff:
    return Staff(
        staff_id=_get(row, mapping, "staff_id"),
        name=_get(row, mapping, "name"),
        role=_get(row, mapping, "role"),
    )


def row_to_line_item(row: dict, mapping: dict[str, str]) -> LineItem:
    return LineItem(
        item_sku=_get(row, mapping, "item_sku"),
        item_name=_get(row, mapping, "item_name"),
        category=_get(row, mapping, "category"),
        qty=int(_get(row, mapping, "qty")),
        unit_price=float(_get(row, mapping, "unit_price")),
        line_amount=float(_get(row, mapping, "line_amount")),
        discount_amount=float(_get(row, mapping, "discount_amount") or "0"),
        is_void=_bool(_get(row, mapping, "is_void")),
        void_after_fire=_bool(_get(row, mapping, "void_after_fire")),
        is_comp=_bool(_get(row, mapping, "is_comp")),
    )


def rows_to_orders(rows: list[dict], mapping: dict[str, str]) -> list[Order]:
    """Group flat sales-detail rows (one per line item) into canonical Orders."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[_get(row, mapping, "order_id")].append(row)

    orders: list[Order] = []
    for oid, group in groups.items():
        first = group[0]
        line_items = [row_to_line_item(row, mapping) for row in group]
        payment = Payment(
            method=_get(first, mapping, "payment_method"),
            amount=float(_get(first, mapping, "payment_amount")),
            tax_rate=float(_get(first, mapping, "tax_rate")),
        )
        orders.append(Order(
            order_id=oid,
            datetime=dt.fromisoformat(_get(first, mapping, "datetime")),
            staff_id=_get(first, mapping, "staff_id"),
            staff_name=_get(first, mapping, "staff_name"),
            table=_get(first, mapping, "table"),
            channel=_get(first, mapping, "channel"),
            order_status=_get(first, mapping, "order_status"),
            customer_ref=_get(first, mapping, "customer_ref"),
            line_items=line_items,
            payments=[payment],
        ))
    return orders


# ── File loaders ──

def load_menu(path: Path, mapping: dict[str, str] | None = None) -> dict[str, MenuItem]:
    m = mapping or default_mapping.MENU
    with open(path, newline="") as f:
        items = [row_to_menu_item(row, m) for row in csv.DictReader(f)]
    return {item.sku: item for item in items}


def load_staff(path: Path, mapping: dict[str, str] | None = None) -> dict[str, Staff]:
    m = mapping or default_mapping.STAFF
    with open(path, newline="") as f:
        staff = [row_to_staff(row, m) for row in csv.DictReader(f)]
    return {s.staff_id: s for s in staff}


def load_orders(
    path: Path,
    mapping: dict[str, str] | None = None,
) -> list[Order]:
    m = mapping or default_mapping.SALES_DETAIL
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows_to_orders(rows, m)


def load_reviews(
    path: Path,
    mapping: dict[str, str] | None = None,
) -> list[Review]:
    m = mapping or default_mapping.REVIEWS
    reviews: list[Review] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            reviews.append(Review(
                review_id=_get(row, m, "review_id"),
                source=_get(row, m, "source"),
                rating=int(_get(row, m, "rating")),
                posted_at=dt.fromisoformat(_get(row, m, "posted_at")),
                reviewer_name=_get(row, m, "reviewer_name"),
                text=_get(row, m, "text"),
            ))
    return reviews


def load_dataset(
    sales_path: Path,
    menu_path: Path,
    staff_path: Path,
    mapping_module: Any = None,
) -> tuple[list[Order], dict[str, MenuItem], dict[str, Staff]]:
    mod = mapping_module or default_mapping
    orders = load_orders(sales_path, mod.SALES_DETAIL)
    menu = load_menu(menu_path, mod.MENU)
    staff = load_staff(staff_path, mod.STAFF)
    return orders, menu, staff
