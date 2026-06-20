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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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


# Default ladder: completing a card promotes the customer to the next tier,
# which carries a better reward. The top tier loops (resets) forever.
DEFAULT_TIERS: list[Tier] = [
    Tier(name="Silver", stamps_required=5, reward="a free ice cream"),
    Tier(name="Gold", stamps_required=8, reward="a free sundae"),
    Tier(name="Platinum", stamps_required=10, reward="a free dessert platter"),
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
    updated_at: str = ""
    rewards: list[Reward] = field(default_factory=list)

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
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def all(self) -> list[LoyaltyCard]:
        return [LoyaltyCard.from_dict(v) for v in self._load().values()]


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
