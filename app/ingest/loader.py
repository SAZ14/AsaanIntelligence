from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime as dt
from pathlib import Path
from typing import Any

from app.models.canonical import LineItem, LoyaltyCustomer, MenuItem, Order, Payment, Review, Staff
from app.ingest.mappings import cafe_generic as default_mapping

CUSTOMER_CSV_FIELDS = ["customer_ref", "qr_token", "display_name", "phone", "channel", "opted_in"]


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


def load_customers(
    path: Path,
    mapping: dict[str, str] | None = None,
) -> dict[str, LoyaltyCustomer]:
    """Load QR-linked loyalty profiles. Returns empty dict if file missing."""
    if not path.exists():
        return {}
    m = mapping or default_mapping.CUSTOMERS
    customers: dict[str, LoyaltyCustomer] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            cref = _get(row, m, "customer_ref")
            if not cref:
                continue
            customers[cref] = LoyaltyCustomer(
                customer_ref=cref,
                qr_token=_get(row, m, "qr_token"),
                display_name=_get(row, m, "display_name"),
                phone=_get(row, m, "phone"),
                channel=_get(row, m, "channel") or "sms",
                opted_in=_bool(_get(row, m, "opted_in") or "true"),
            )
    return customers


def save_customers(
    path: Path,
    customers: dict[str, LoyaltyCustomer],
    mapping: dict[str, str] | None = None,
) -> None:
    """Persist QR-linked loyalty profiles to CSV."""
    m = mapping or default_mapping.CUSTOMERS
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CUSTOMER_CSV_FIELDS)
        writer.writeheader()
        for cref in sorted(customers.keys()):
            c = customers[cref]
            writer.writerow({
                m["customer_ref"]: c.customer_ref,
                m["qr_token"]: c.qr_token,
                m["display_name"]: c.display_name,
                m["phone"]: c.phone,
                m["channel"]: c.channel,
                m["opted_in"]: "true" if c.opted_in else "false",
            })


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
