"""Shared API dependencies."""

from __future__ import annotations

import os
from pathlib import Path

from app.community.store import load_venue_config
from app.ingest import load_dataset
from fastapi import Request, HTTPException
from twilio.request_validator import RequestValidator


def data_dir() -> Path:
    return Path(os.environ.get(
        "ASAAN_DATA_DIR",
        Path(__file__).resolve().parent.parent.parent / "data",
    ))


def outbox_dir() -> Path:
    return Path(os.environ.get(
        "ASAAN_OUTBOX_DIR",
        Path(__file__).resolve().parent.parent.parent / "output",
    ))


def menu_path() -> Path:
    return data_dir() / "menu.csv"


def sales_path() -> Path:
    return data_dir() / "sales_detail.csv"


def staff_path() -> Path:
    return data_dir() / "staff.csv"


def load_orders():
    d = data_dir()
    return load_dataset(d / "sales_detail.csv", d / "menu.csv", d / "staff.csv")


def venue_name() -> str:
    return load_venue_config().venue_name


async def validate_twilio_request(request: Request) -> None:
    validator = RequestValidator(os.environ.get("TWILIO_AUTH_TOKEN", ""))
    
    base_url = os.environ.get("TWILIO_WEBHOOK_BASE_URL")
    if base_url:
        url = base_url.rstrip("/") + request.url.path
    else:
        proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
        host = request.headers.get("X-Forwarded-Host", request.headers.get("host", request.url.netloc))
        url = f"{proto}://{host}{request.url.path}"
        
    form_data = await request.form()
    params = {k: v for k, v in form_data.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    
    if not validator.validate(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
