from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime as dt
from pathlib import Path
from typing import Any

from app.models.canonical import LineItem, MenuItem, Order, Payment, Staff
from app.ingest.mappings import cafe_generic as default_mapping


def _get(row: dict[str, str], mapping: dict[str, str], key: str) -> str:
    return row.get(mapping[key], "").strip()


def load_menu(path: Path, mapping: dict[str, str] | None = None) -> dict[str, MenuItem]:
    m = mapping or default_mapping.MENU
    items: dict[str, MenuItem] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            sku = _get(row, m, "sku")
            cost_raw = _get(row, m, "cost")
            items[sku] = MenuItem(
                sku=sku,
                name=_get(row, m, "name"),
                category=_get(row, m, "category"),
                cost=float(cost_raw) if cost_raw else None,
                price=float(_get(row, m, "price")),
            )
    return items


def load_staff(path: Path, mapping: dict[str, str] | None = None) -> dict[str, Staff]:
    m = mapping or default_mapping.STAFF
    staff: dict[str, Staff] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            sid = _get(row, m, "staff_id")
            staff[sid] = Staff(
                staff_id=sid,
                name=_get(row, m, "name"),
                role=_get(row, m, "role"),
            )
    return staff


def _bool(val: str) -> bool:
    return val.strip() in ("1", "True", "true", "yes")


def load_orders(
    path: Path,
    mapping: dict[str, str] | None = None,
) -> list[Order]:
    m = mapping or default_mapping.SALES_DETAIL
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            oid = _get(row, m, "order_id")
            groups[oid].append(row)

    orders: list[Order] = []
    for oid, rows in groups.items():
        first = rows[0]
        line_items: list[LineItem] = []
        for row in rows:
            line_items.append(LineItem(
                item_sku=_get(row, m, "item_sku"),
                item_name=_get(row, m, "item_name"),
                category=_get(row, m, "category"),
                qty=int(_get(row, m, "qty")),
                unit_price=float(_get(row, m, "unit_price")),
                line_amount=float(_get(row, m, "line_amount")),
                discount_amount=float(_get(row, m, "discount_amount") or "0"),
                is_void=_bool(_get(row, m, "is_void")),
                void_after_fire=_bool(_get(row, m, "void_after_fire")),
                is_comp=_bool(_get(row, m, "is_comp")),
            ))

        payment = Payment(
            method=_get(first, m, "payment_method"),
            amount=float(_get(first, m, "payment_amount")),
            tax_rate=float(_get(first, m, "tax_rate")),
        )

        orders.append(Order(
            order_id=oid,
            datetime=dt.fromisoformat(_get(first, m, "datetime")),
            staff_id=_get(first, m, "staff_id"),
            staff_name=_get(first, m, "staff_name"),
            table=_get(first, m, "table"),
            channel=_get(first, m, "channel"),
            order_status=_get(first, m, "order_status"),
            customer_ref=_get(first, m, "customer_ref"),
            line_items=line_items,
            payments=[payment],
        ))

    return orders


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
