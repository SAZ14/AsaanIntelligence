"""Column mapping for the generic café POS export format.

To support a new POS format, duplicate this file, change the column names
in the dicts below, and pass the new mapping module to the loader.
"""

SALES_DETAIL = {
    "order_id": "order_id",
    "datetime": "datetime",
    "staff_id": "staff_id",
    "staff_name": "staff_name",
    "table": "table",
    "channel": "channel",
    "item_sku": "item_sku",
    "item_name": "item_name",
    "category": "category",
    "qty": "qty",
    "unit_price": "unit_price",
    "line_amount": "line_amount",
    "discount_amount": "discount_amount",
    "is_void": "is_void",
    "void_after_fire": "void_after_fire",
    "is_comp": "is_comp",
    "order_status": "order_status",
    "payment_method": "payment_method",
    "payment_amount": "payment_amount",
    "tax_rate": "tax_rate",
    "customer_ref": "customer_ref",
}

MENU = {
    "sku": "sku",
    "name": "name",
    "category": "category",
    "cost": "cost",
    "price": "price",
}

STAFF = {
    "staff_id": "staff_id",
    "name": "name",
    "role": "role",
}

REVIEWS = {
    "review_id": "review_id",
    "source": "source",
    "rating": "rating",
    "posted_at": "posted_at",
    "reviewer_name": "reviewer_name",
    "text": "text",
}

INGREDIENTS = {
    "ingredient_id": "ingredient_id",
    "name": "name",
    "unit": "unit",
    "unit_cost": "unit_cost",
    "reorder_level": "reorder_level",
    "pack_unit": "pack_unit",
    "pack_size": "pack_size",
}

RECIPES = {
    "sku": "sku",
    "ingredient_id": "ingredient_id",
    "qty_per_unit": "qty_per_unit",
}

STOCK_RECEIPTS = {
    "receipt_id": "receipt_id",
    "datetime": "datetime",
    "ingredient_id": "ingredient_id",
    "qty": "qty",
    "unit_cost": "unit_cost",
    "unit": "unit",
}
