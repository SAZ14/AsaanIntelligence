"""Shared API dependencies."""

from __future__ import annotations

import os
from pathlib import Path

from app.community.store import load_venue_config
from app.ingest import load_dataset


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
