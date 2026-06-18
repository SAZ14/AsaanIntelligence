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


class LoyaltyCustomer(BaseModel):
    """QR-linked guest profile — ties a scan token to POS customer_ref."""

    customer_ref: str
    qr_token: str = ""
    display_name: str = ""
    phone: str = ""
    channel: str = "sms"  # sms | whatsapp
    opted_in: bool = True


class LoyaltyRules(BaseModel):
    """Merchant-configurable loyalty incentive rules."""

    milestone_visit_interval: int = 5
    milestone_discount_pct: float = 10
    winback_lapsed_discount_pct: float = 15
    winback_lapsing_discount_pct: float = 10
    corporate_discount_pct: float = 5
    streak_window_days: int = 7
    streak_min_visits: int = 3
    streak_reward: str = "complimentary dessert on your next visit"
