"""Customer agent — WhatsApp digital loyalty stamp-card engine.

This replaces the previous retention / win-back Customer agent. The model is
now a loyalty programme driven entirely by QR scans:

    customer scans the venue QR  →  it opens WhatsApp to the business (a wa.me
    click-to-chat link)  →  sending the pre-filled message registers ONE scan
    →  the agent adds a stamp and replies with a plain-text progress message,
    delivered straight to the customer's WhatsApp.

When every stamp on a card is filled the customer unlocks that tier's reward
(handed over manually by staff via a phone-number lookup) and is automatically
promoted to the next, better tier.

Design choices
--------------
- **Identity = the customer's WhatsApp number.** The wa.me click-to-chat flow
  captures it on the very first scan, so there is no signup, no payment token
  and no name to manage.
- **No LLM anywhere.** Every reply is a deterministic template, so each scan
  costs nothing, never rate-limits and always responds instantly. (This is a
  hard requirement — keep it that way.)
- **Plain text only** — a unicode progress bar, not a rendered card image.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


# ── Programme configuration ──

@dataclass(frozen=True)
class Tier:
    """One card in the loyalty ladder."""
    name: str
    stamps_required: int
    reward: str


# Start simple: ONE card — collect 5 stamps for a reward, then the card resets
# and repeats. The engine already supports a multi-tier ladder (completing a
# card promotes to the next, better tier), but we default to a single tier and
# tweak rewards/stamp counts per restaurant. Add tiers later when wanted.
DEFAULT_TIERS: list[Tier] = [
    Tier(name="Loyalty", stamps_required=5, reward="a free treat"),
]

DEFAULT_PREFILL = "Hi! I'd like to collect my loyalty stamp \U0001F3AB"


# ── Stored state ──

@dataclass
class Reward:
    """A reward the customer has unlocked but may not yet have collected."""
    tier_name: str
    reward: str
    earned_at: str                 # ISO timestamp
    redeemed: bool = False
    redeemed_at: str | None = None


@dataclass
class LoyaltyCard:
    """A customer's loyalty state, keyed by their WhatsApp number."""
    phone: str
    tier_index: int = 0            # position in the tier ladder (current card)
    stamps: int = 0               # stamps on the CURRENT card
    total_scans: int = 0          # lifetime scans
    created_at: str = ""
    updated_at: str = ""              # ISO timestamp of the last scan
    rewards: list[Reward] = field(default_factory=list)
    last_nudged_at: str | None = None  # last re-engagement message (anti-spam)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LoyaltyCard":
        return cls(
            phone=d["phone"],
            tier_index=d.get("tier_index", 0),
            stamps=d.get("stamps", 0),
            total_scans=d.get("total_scans", 0),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            rewards=[Reward(**r) for r in d.get("rewards", [])],
            last_nudged_at=d.get("last_nudged_at"),
        )


@dataclass
class ScanResult:
    """Outcome of a single scan — the updated card and the reply to send."""
    card: LoyaltyCard
    message: str
    is_first_scan: bool = False
    completed_tier: Tier | None = None   # the card just completed (if any)
    promoted_to: Tier | None = None      # the tier of the fresh card afterwards


# ── Persistence ──

class CardStore(Protocol):
    """Anything that can persist loyalty cards by phone number."""
    def get(self, phone: str) -> LoyaltyCard | None: ...
    def put(self, card: LoyaltyCard) -> None: ...
    def all(self) -> list[LoyaltyCard]: ...


@dataclass
class InMemoryCardStore:
    """Non-persistent store — used by tests and the demo runner."""
    _cards: dict[str, LoyaltyCard] = field(default_factory=dict)

    def get(self, phone: str) -> LoyaltyCard | None:
        return self._cards.get(phone)

    def put(self, card: LoyaltyCard) -> None:
        self._cards[card.phone] = card

    def all(self) -> list[LoyaltyCard]:
        return list(self._cards.values())


@dataclass
class JsonCardStore:
    """File-backed store — survives across processes (used by the webhook)."""
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def get(self, phone: str) -> LoyaltyCard | None:
        raw = self._load().get(phone)
        return LoyaltyCard.from_dict(raw) if raw else None

    def put(self, card: LoyaltyCard) -> None:
        data = self._load()
        data[card.phone] = card.to_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def all(self) -> list[LoyaltyCard]:
        return [LoyaltyCard.from_dict(v) for v in self._load().values()]


@dataclass
class SqliteCardStore:
    """SQLite-backed store — the recommended production store.

    SQLite is built into Python (no dependency, no server, no cost): the whole
    database is a single file on disk. One shared database holds every
    restaurant's cards in a ``cards`` table keyed by ``(restaurant_id, phone)``,
    so a SqliteCardStore is scoped to one ``restaurant_id`` and only ever sees
    its own venue's cards. Writes are transactional, so two scans landing at the
    same instant can't corrupt a count (unlike the plain JSON store).

    The table is created lazily on first use, so merely constructing a store
    (e.g. to read a venue's config for QR generation) touches no disk.
    """
    db_path: str | Path
    restaurant_id: str = "default"
    _ready: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        if self.db_path.parent and not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cards (
                    restaurant_id TEXT NOT NULL,
                    phone         TEXT NOT NULL,
                    tier_index    INTEGER NOT NULL DEFAULT 0,
                    stamps        INTEGER NOT NULL DEFAULT 0,
                    total_scans   INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT,
                    updated_at    TEXT,
                    rewards       TEXT NOT NULL DEFAULT '[]',
                    last_nudged_at TEXT,
                    PRIMARY KEY (restaurant_id, phone)
                )
                """
            )
            # Migrate databases created before last_nudged_at existed.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)").fetchall()}
            if "last_nudged_at" not in cols:
                conn.execute("ALTER TABLE cards ADD COLUMN last_nudged_at TEXT")
            conn.execute("PRAGMA journal_mode=WAL")  # better concurrent reads/writes
            conn.commit()
            self._ready = True
        return conn

    @staticmethod
    def _row_to_card(row: sqlite3.Row) -> LoyaltyCard:
        return LoyaltyCard(
            phone=row["phone"],
            tier_index=row["tier_index"],
            stamps=row["stamps"],
            total_scans=row["total_scans"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            rewards=[Reward(**r) for r in json.loads(row["rewards"])],
            last_nudged_at=row["last_nudged_at"],
        )

    def get(self, phone: str) -> LoyaltyCard | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM cards WHERE restaurant_id = ? AND phone = ?",
                (self.restaurant_id, phone),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_card(row) if row else None

    def put(self, card: LoyaltyCard) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO cards (restaurant_id, phone, tier_index, stamps,
                                   total_scans, created_at, updated_at, rewards,
                                   last_nudged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(restaurant_id, phone) DO UPDATE SET
                    tier_index    = excluded.tier_index,
                    stamps        = excluded.stamps,
                    total_scans   = excluded.total_scans,
                    created_at    = excluded.created_at,
                    updated_at    = excluded.updated_at,
                    rewards       = excluded.rewards,
                    last_nudged_at = excluded.last_nudged_at
                """,
                (
                    self.restaurant_id, card.phone, card.tier_index, card.stamps,
                    card.total_scans, card.created_at, card.updated_at,
                    json.dumps([asdict(r) for r in card.rewards]),
                    card.last_nudged_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def all(self) -> list[LoyaltyCard]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM cards WHERE restaurant_id = ?", (self.restaurant_id,)
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_card(r) for r in rows]


# ── Message formatting (deterministic, no LLM) ──

def _progress_bar(stamps: int, required: int) -> str:
    filled = "▰" * max(0, stamps)
    empty = "▱" * max(0, required - stamps)
    return f"[{filled}{empty}] {stamps}/{required}"


# ── The loyalty programme ──

@dataclass
class LoyaltyProgram:
    venue_name: str = "Sugar Rush"
    tiers: list[Tier] = field(default_factory=lambda: list(DEFAULT_TIERS))
    store: CardStore = field(default_factory=InMemoryCardStore)

    # -- helpers --
    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def current_tier(self, card: LoyaltyCard) -> Tier:
        return self.tiers[min(card.tier_index, len(self.tiers) - 1)]

    # -- the one entry point the QR flow calls --
    def record_scan(self, phone: str) -> ScanResult:
        """Register one scan for ``phone`` and return the reply to send back."""
        phone = phone.strip()
        existing = self.store.get(phone)
        first = existing is None
        card = existing or LoyaltyCard(phone=phone, created_at=self._now())

        card.stamps += 1
        card.total_scans += 1
        card.updated_at = self._now()

        tier = self.current_tier(card)
        completed: Tier | None = None
        promoted: Tier | None = None

        if card.stamps >= tier.stamps_required:
            completed = tier
            card.rewards.append(
                Reward(tier_name=tier.name, reward=tier.reward, earned_at=self._now())
            )
            # Promote to the next tier; the top tier loops on itself.
            if card.tier_index < len(self.tiers) - 1:
                card.tier_index += 1
            card.stamps = 0
            promoted = self.current_tier(card)

        self.store.put(card)
        message = self._build_message(card, tier, completed, promoted, first)
        return ScanResult(
            card=card, message=message, is_first_scan=first,
            completed_tier=completed, promoted_to=promoted,
        )

    def _build_message(
        self,
        card: LoyaltyCard,
        worked_tier: Tier,
        completed: Tier | None,
        promoted: Tier | None,
        first: bool,
    ) -> str:
        venue = self.venue_name

        if completed is not None and promoted is not None:
            lines = [
                f"\U0001F3C6 Card complete at {venue}! You've unlocked "
                f"{completed.reward.upper()}.",
                "Show this message to our staff to claim it. \U0001F381",
            ]
            if promoted.name != completed.name:
                lines.append(
                    f"You've leveled up to a {promoted.name} card — collect "
                    f"{promoted.stamps_required} stamps for {promoted.reward}!"
                )
            else:
                lines.append(
                    f"Your {promoted.name} card has reset — collect "
                    f"{promoted.stamps_required} more for {promoted.reward} again!"
                )
            lines.append(_progress_bar(0, promoted.stamps_required))
            return "\n".join(lines)

        required = worked_tier.stamps_required
        if first:
            return "\n".join([
                f"\U0001F389 Welcome to {venue} Rewards!",
                f"You earned your 1st stamp on your {worked_tier.name} card.",
                _progress_bar(card.stamps, required),
                f"Collect {required} stamps and {worked_tier.reward} is on us — "
                "scan the QR on every visit to fill it up!",
            ])

        remaining = required - card.stamps
        nudge = (
            "Just 1 more to go!" if remaining == 1
            else f"{remaining} more and you've earned {worked_tier.reward}."
        )
        return "\n".join([
            f"⭐ Stamp added at {venue}!",
            _progress_bar(card.stamps, required),
            f"{nudge} See you soon!",
        ])

    # -- staff-facing (manual redemption) --
    def lookup(self, phone: str) -> LoyaltyCard | None:
        return self.store.get(phone.strip())

    @staticmethod
    def pending_rewards(card: LoyaltyCard) -> list[Reward]:
        return [r for r in card.rewards if not r.redeemed]

    def redeem(self, phone: str, tier_name: str | None = None) -> Reward | None:
        """Mark the customer's oldest unredeemed reward as given. Staff action."""
        card = self.store.get(phone.strip())
        if card is None:
            return None
        for r in card.rewards:
            if not r.redeemed and (tier_name is None or r.tier_name == tier_name):
                r.redeemed = True
                r.redeemed_at = self._now()
                self.store.put(card)
                return r
        return None


def format_card_status(card: LoyaltyCard, program: LoyaltyProgram) -> str:
    """Plain-text summary for staff looking a customer up by phone."""
    tier = program.current_tier(card)
    pending = program.pending_rewards(card)
    lines = [
        f"Customer {card.phone}",
        f"Current card: {tier.name}  {_progress_bar(card.stamps, tier.stamps_required)}",
        f"Lifetime scans: {card.total_scans}",
    ]
    if pending:
        lines.append("REWARDS TO HAND OVER:")
        lines.extend(f"  • {r.reward}  ({r.tier_name}, earned {r.earned_at})" for r in pending)
    else:
        lines.append("No rewards pending — nothing to hand over.")
    return "\n".join(lines)


def build_wa_link(business_number: str, prefilled_text: str = DEFAULT_PREFILL) -> str:
    """The wa.me click-to-chat URL the printed QR should encode.

    Scanning it opens WhatsApp to the venue with ``prefilled_text`` ready to
    send; sending it is what registers a scan.
    """
    digits = "".join(ch for ch in business_number.replace("whatsapp:", "") if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(prefilled_text)}"


# ── Re-engagement & VIP / events ──
#
# Built on the data every scan already records (total_scans + updated_at per
# customer per venue). Two jobs:
#   1. Win back loyal regulars who've gone quiet — a "we miss you" nudge.
#   2. Surface the most loyal customers so the venue can invite them to events.
#
# NOTE ON WHATSAPP: these are PROACTIVE messages, usually sent days after the
# customer's last message, i.e. outside WhatsApp's 24h service window — so in
# production they must be sent as a Meta-approved message *template*. The code
# is identical; you just register the wording as a template in Twilio. The
# instant stamp reply (webhook) needs no template; only these pushes do.

DEFAULT_LOYAL_MIN_SCANS = 3      # "loyal" = at least this many lifetime scans
DEFAULT_INACTIVE_DAYS = 5        # quiet for this long → eligible for a nudge
DEFAULT_NUDGE_COOLDOWN_DAYS = 5  # don't nudge the same person again this soon


def _iso_to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def days_since_last_scan(card: LoyaltyCard, today: date | None = None) -> int | None:
    today = today or date.today()
    last = _iso_to_date(card.updated_at)
    return (today - last).days if last else None


def is_loyal(card: LoyaltyCard, min_scans: int = DEFAULT_LOYAL_MIN_SCANS) -> bool:
    return card.total_scans >= min_scans


@dataclass
class EngagementCandidate:
    card: LoyaltyCard
    days_inactive: int
    message: str


def format_miss_you_message(venue: str, days_inactive: int | None = None) -> str:
    gap = f"It's been {days_inactive} days — " if days_inactive else ""
    return (
        f"Hey, we miss you at {venue}! \U0001F60A\n"
        f"{gap}come back in for the best meal of your day. Your loyalty card is "
        "waiting — your next scan gets you closer to a free treat. \U0001F381"
    )


def format_event_invite(venue: str, event_details: str) -> str:
    return (
        f"\U0001F389 You're one of {venue}'s most loyal regulars — so you're invited!\n"
        f"{event_details}\n"
        "Reply YES to reserve your spot. See you there!"
    )


def at_risk_loyal_customers(
    program: LoyaltyProgram,
    *,
    min_scans: int = DEFAULT_LOYAL_MIN_SCANS,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    cooldown_days: int = DEFAULT_NUDGE_COOLDOWN_DAYS,
    today: date | None = None,
) -> list[EngagementCandidate]:
    """Loyal customers who've gone quiet and aren't on nudge cooldown.

    Sorted most-loyal first. Pure read — sending/marking is a separate step so
    you can preview before committing.
    """
    today = today or date.today()
    out: list[EngagementCandidate] = []
    for card in program.store.all():
        if not is_loyal(card, min_scans):
            continue
        gap = days_since_last_scan(card, today)
        if gap is None or gap < inactive_days:
            continue
        nudged = _iso_to_date(card.last_nudged_at)
        if nudged is not None and (today - nudged).days < cooldown_days:
            continue
        out.append(EngagementCandidate(card, gap, format_miss_you_message(program.venue_name, gap)))
    out.sort(key=lambda c: c.card.total_scans, reverse=True)
    return out


def send_reengagement(
    program: LoyaltyProgram,
    notifier,
    candidates: list[EngagementCandidate],
    *,
    today: date | None = None,
) -> list:
    """Send each nudge and stamp ``last_nudged_at`` so they aren't re-spammed."""
    today = today or date.today()
    sent = []
    for c in candidates:
        sent.append(notifier.send(c.card.phone, c.message))
        c.card.last_nudged_at = today.isoformat()
        program.store.put(c.card)
    return sent


def top_loyal_customers(program: LoyaltyProgram, n: int = 10) -> list[LoyaltyCard]:
    """The most loyal customers (by lifetime scans, then rewards earned)."""
    cards = program.store.all()
    cards.sort(key=lambda c: (c.total_scans, len(c.rewards)), reverse=True)
    return cards[:n]


def send_event_invites(notifier, venue: str, customers: list[LoyaltyCard], event_details: str) -> list:
    return [notifier.send(c.phone, format_event_invite(venue, event_details)) for c in customers]


# ── Multi-restaurant support ──
#
# One deployment serves many restaurants. Each has its own WhatsApp number (so
# its own QR), its own reward config, and its own card store — cards never mix
# between venues. Inbound messages are routed to the right restaurant by the
# number the customer messaged (Twilio's ``To`` field).

def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


@dataclass
class Restaurant:
    """A single venue: its WhatsApp identity + its own loyalty programme."""
    id: str
    whatsapp_number: str
    program: LoyaltyProgram
    # Owner-configurable re-engagement policy (set per venue in restaurants.json;
    # these are the defaults until the owner changes them).
    inactive_days: int = DEFAULT_INACTIVE_DAYS
    min_scans: int = DEFAULT_LOYAL_MIN_SCANS
    nudge_cooldown_days: int = DEFAULT_NUDGE_COOLDOWN_DAYS

    @property
    def name(self) -> str:
        return self.program.venue_name

    def wa_link(self, prefilled_text: str = DEFAULT_PREFILL) -> str:
        return build_wa_link(self.whatsapp_number, prefilled_text)

    def reward_line(self) -> str:
        """A short call-to-action for the QR poster, e.g. '5 stamps = a free X'."""
        t = self.program.tiers[0]
        return f"{t.stamps_required} stamps = {t.reward}"


def build_restaurant(
    id: str,
    name: str,
    whatsapp_number: str,
    *,
    stamps_required: int = 5,
    reward: str = "a free treat",
    tiers: list[Tier] | None = None,
    store: CardStore | None = None,
    db_path: str | Path | None = None,
    store_dir: str | Path | None = None,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    min_scans: int = DEFAULT_LOYAL_MIN_SCANS,
    nudge_cooldown_days: int = DEFAULT_NUDGE_COOLDOWN_DAYS,
) -> Restaurant:
    """Build one restaurant and pick where its cards persist.

    Storage precedence:
      1. ``store``      — an explicit CardStore (tests / custom backends)
      2. ``db_path``    — SQLite database shared by all venues (recommended)
      3. ``store_dir``  — legacy JSON file per venue (``<store_dir>/<id>.json``)
      4. in-memory      — nothing persisted (demo)

    ``inactive_days`` / ``min_scans`` / ``nudge_cooldown_days`` are the
    owner-set re-engagement policy for this venue.
    """
    if tiers is None:
        tiers = [Tier(name="Loyalty", stamps_required=stamps_required, reward=reward)]
    if store is None:
        if db_path is not None:
            store = SqliteCardStore(db_path, restaurant_id=id)
        elif store_dir is not None:
            store = JsonCardStore(Path(store_dir) / f"{id}.json")
        else:
            store = InMemoryCardStore()
    program = LoyaltyProgram(venue_name=name, tiers=tiers, store=store)
    return Restaurant(
        id=id, whatsapp_number=whatsapp_number, program=program,
        inactive_days=inactive_days, min_scans=min_scans,
        nudge_cooldown_days=nudge_cooldown_days,
    )


@dataclass
class Registry:
    """All restaurants, routable by WhatsApp number or id."""
    restaurants: list[Restaurant] = field(default_factory=list)

    def by_number(self, whatsapp_number: str) -> Restaurant | None:
        key = _digits(whatsapp_number)
        return next((r for r in self.restaurants if _digits(r.whatsapp_number) == key), None)

    def by_id(self, restaurant_id: str) -> Restaurant | None:
        return next((r for r in self.restaurants if r.id == restaurant_id), None)

    def all(self) -> list[Restaurant]:
        return list(self.restaurants)


def load_registry(
    config_path: str | Path,
    db_path: str | Path | None = "loyalty.db",
    store_dir: str | Path | None = None,
) -> Registry:
    """Build a Registry from a JSON list of restaurants.

    Each entry: ``{"id", "name", "whatsapp_number", "stamps_required"?,
    "reward"?, "tiers"? [{"name","stamps_required","reward"}, ...]}``.

    Pass ``store_dir`` to use the legacy per-venue JSON files; otherwise all
    venues share the single SQLite database at ``db_path``.
    """
    data = json.loads(Path(config_path).read_text())
    restaurants = [
        build_restaurant(
            id=e["id"],
            name=e["name"],
            whatsapp_number=e["whatsapp_number"],
            stamps_required=e.get("stamps_required", 5),
            reward=e.get("reward", "a free treat"),
            tiers=[Tier(**t) for t in e["tiers"]] if e.get("tiers") else None,
            db_path=None if store_dir else db_path,
            store_dir=store_dir,
            inactive_days=e.get("inactive_days", DEFAULT_INACTIVE_DAYS),
            min_scans=e.get("min_scans", DEFAULT_LOYAL_MIN_SCANS),
            nudge_cooldown_days=e.get("nudge_cooldown_days", DEFAULT_NUDGE_COOLDOWN_DAYS),
        )
        for e in data
    ]
    return Registry(restaurants=restaurants)


def build_default_registry(
    config_path: str | Path | None = None,
    db_path: str | Path = "loyalty.db",
) -> Registry:
    """Shared entry point for the webhook and the staff scripts.

    Storage defaults to a single SQLite database (env ``LOYALTY_DB``, default
    ``loyalty.db``). Uses the restaurants config file if present (env
    ``RESTAURANTS_CONFIG``, default ``restaurants.json``); otherwise falls back
    to a single restaurant described by env vars, so a fresh checkout runs with
    zero config.
    """
    config_path = Path(config_path or os.environ.get("RESTAURANTS_CONFIG", "restaurants.json"))
    db_path = Path(os.environ.get("LOYALTY_DB", str(db_path)))
    if config_path.exists():
        return load_registry(config_path, db_path=db_path)
    return Registry([
        build_restaurant(
            id=os.environ.get("VENUE_ID", "default"),
            name=os.environ.get("VENUE_NAME", "Sugar Rush"),
            whatsapp_number=os.environ.get("WHATSAPP_FROM", "whatsapp:+14155238886"),
            db_path=db_path,
        )
    ])
