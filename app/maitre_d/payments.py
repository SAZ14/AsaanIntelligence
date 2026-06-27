"""Deposit payments for high-no-show-risk holds.

The agent never talks to a payment processor directly — it goes through a small
:class:`PaymentProvider` interface so a real gateway (Stripe, Checkout, a local
PSP) can be dropped in without touching the decision engine. The bundled
:class:`StubPaymentProvider` issues deterministic links and confirms them via a
plain webhook payload, so the whole deposit flow runs end-to-end offline and in
tests.

Flow:
    1. agent asks the provider for a checkout link (``create_checkout``);
    2. the link + a ``payment_ref`` are sent to the guest and stored on the
       reservation;
    3. when the guest pays, the gateway calls our payment webhook; we hand the
       payload to ``parse_webhook`` → ``(payment_ref, paid)`` and, if paid, flip
       the reservation ``pending`` → ``confirmed``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class PaymentLink:
    url: str
    ref: str          # opaque id we store on the reservation and match on webhook


@runtime_checkable
class PaymentProvider(Protocol):
    def create_checkout(
        self, reservation_id: str, amount: int, currency: str
    ) -> PaymentLink:
        """Create a checkout session and return its URL + reference."""
        ...

    def parse_webhook(self, payload: dict) -> tuple[str, bool]:
        """Map a gateway webhook payload to ``(payment_ref, paid)``."""
        ...


class StubPaymentProvider:
    """A no-network provider: real-looking links, webhook-confirmable.

    Swap for a real gateway in production; the agent code does not change.
    """

    def __init__(self, base_url: str = "https://pay.example/checkout") -> None:
        self.base_url = base_url.rstrip("/")

    def create_checkout(
        self, reservation_id: str, amount: int, currency: str
    ) -> PaymentLink:
        ref = f"pay_{uuid.uuid4().hex[:16]}"
        url = f"{self.base_url}/{ref}?amount={amount}&currency={currency}"
        return PaymentLink(url=url, ref=ref)

    def parse_webhook(self, payload: dict) -> tuple[str, bool]:
        ref = str(payload.get("ref") or payload.get("payment_ref") or "")
        status = str(payload.get("status", "")).lower()
        paid = status in ("paid", "succeeded", "completed", "success")
        return ref, paid
