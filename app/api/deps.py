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


def members_path() -> Path:
    return data_dir() / "community_members.csv"


def redeem_codes_path() -> Path:
    return data_dir() / "redeem_codes.jsonl"


def stamp_events_path() -> Path:
    return data_dir() / "stamp_events.jsonl"


def venue_config_path() -> Path:
    return data_dir() / "venue_config.json"


def deals_path() -> Path:
    return data_dir() / "deals.json"


def onboarding_sessions_path() -> Path:
    return data_dir() / "onboarding_sessions.json"


def menu_path() -> Path:
    return data_dir() / "menu.csv"


def sales_path() -> Path:
    return data_dir() / "sales_detail.csv"


def staff_path() -> Path:
    return data_dir() / "staff.csv"


def community_paths() -> dict[str, Path]:
    return {
        "members_path": members_path(),
        "redeem_path": redeem_codes_path(),
        "events_path": stamp_events_path(),
        "config_path": venue_config_path(),
        "deals_path": deals_path(),
        "menu_path": menu_path(),
        "sessions_path": onboarding_sessions_path(),
    }


def merchant_paths() -> dict[str, Path]:
    p = community_paths()
    return {
        "config_path": p["config_path"],
        "members_path": p["members_path"],
        "events_path": p["events_path"],
        "menu_path": p["menu_path"],
        "deals_path": p["deals_path"],
        "sales_path": sales_path(),
        "staff_path": staff_path(),
    }


def load_orders():
    d = data_dir()
    return load_dataset(d / "sales_detail.csv", d / "menu.csv", d / "staff.csv")


def venue_name() -> str:
    return load_venue_config(venue_config_path()).venue_name
