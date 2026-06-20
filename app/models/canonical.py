from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field, computed_field


class Venue(BaseModel):
    name: str
    currency: str = "PKR"
    timezone: str = "Asia/Karachi"


class MenuItem(BaseModel):
    sku: str
    name: str
    category: str
    cost: float | None = None
    price: float

    @computed_field
    @property
    def margin(self) -> float | None:
        if self.cost is not None and self.price > 0:
            return round((self.price - self.cost) / self.price, 4)
        return None


class Staff(BaseModel):
    staff_id: str
    name: str
    role: str


class LineItem(BaseModel):
    item_sku: str
    item_name: str
    category: str
    qty: int
    unit_price: float
    line_amount: float
    discount_amount: float = 0.0
    is_void: bool = False
    void_after_fire: bool = False
    is_comp: bool = False


class Payment(BaseModel):
    method: str
    amount: float
    tax_rate: float


class Review(BaseModel):
    review_id: str
    source: str
    rating: int
    posted_at: datetime
    reviewer_name: str
    text: str


class Ingredient(BaseModel):
    """A raw stock item that menu items are made from (chicken, milk, beans)."""
    ingredient_id: str
    name: str
    unit: str  # "piece", "g", "ml", "slice", "can", ...
    unit_cost: float | None = None  # cost per single unit
    reorder_level: float = 0.0  # remaining qty at/below which to reorder


class RecipeComponent(BaseModel):
    """One ingredient line in a menu item's recipe (bill of materials).

    qty_per_unit is how much of `ingredient_id` is consumed to make ONE `sku`.
    e.g. Chicken Sandwich (SAN) -> CHICKEN, qty_per_unit=1.
    """
    sku: str
    ingredient_id: str
    qty_per_unit: float


class StockReceipt(BaseModel):
    """A delivery of an ingredient into stock (450 pieces of chicken arrive)."""
    receipt_id: str
    datetime: datetime
    ingredient_id: str
    qty: float
    unit_cost: float | None = None


class Order(BaseModel):
    order_id: str
    datetime: datetime
    staff_id: str
    staff_name: str
    table: str = ""
    channel: str
    order_status: str
    customer_ref: str = ""
    line_items: list[LineItem] = Field(default_factory=list)
    payments: list[Payment] = Field(default_factory=list)
