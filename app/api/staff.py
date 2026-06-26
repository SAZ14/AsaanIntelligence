"""Staff receipt code API — POS / printer integration."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.community.tokens import issue_redeem_code

router = APIRouter(prefix="/staff", tags=["staff"])


class ReceiptRequest(BaseModel):
    order_id: str = ""


class ReceiptResponse(BaseModel):
    code: str
    order_id: str
    message: str


@router.post("/receipt", response_model=ReceiptResponse)
def issue_receipt_code(req: ReceiptRequest):
    """Issue a single-use redeem code to print on a receipt."""
    entry = issue_redeem_code(order_id=req.order_id)
    return ReceiptResponse(
        code=entry.code,
        order_id=entry.order_id,
        message=f"Text {entry.code} to our WhatsApp to collect your stamp.",
    )
