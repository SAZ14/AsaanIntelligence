"""Multi-café tenancy for the Revenue agent.

One bot ("AsaanPay Rev Agent") sits in every owner's phone. Owners all message
that single number; we identify the café by **who is texting** (their own phone)
and route to that café's private agent — so every owner gets their own revenue
advisor over their own data, with full isolation between cafés.

A tenant (café) is defined by:
    owner_phones   the phone number(s) that own this café (the routing key)
    data_dir       where this café's POS CSVs live
    config_path    this café's campaigns / categories / price knobs (optional)
    db_path        this café's SQLite (subscriptions + campaign log)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from app.revenue.agent import RevenueAgent, RevenueReply
from app.revenue.config import RevenueConfig
from app.revenue.store import Store


def normalize_phone(phone: str) -> str:
    p = (phone or "").strip().lower()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    return "".join(ch for ch in p if ch.isdigit() or ch == "+")


# The shared sample dataset root — never a private café folder.
_SAMPLE_DATA_ROOT = (Path(__file__).resolve().parents[2] / "data").resolve()

_MENU_TEMPLATE = "sku,name,category,cost,price\n"


def provision_cafe_dir(data_dir: str) -> tuple[Path, bool]:
    """Create a café's OWN private POS folder, safely.

    * Refuses the shared sample ``data/`` root (that's not anyone's private data).
    * Creates the folder if missing and drops an empty ``menu.csv`` template so
      it's obvious where this café's files go.
    Returns (path, created_now). Isolation between cafés is still enforced by the
    registry; this just makes the folder step foolproof.
    """
    p = Path(data_dir)
    if p.resolve() == _SAMPLE_DATA_ROOT:
        raise ValueError(
            "data_dir must be a café-specific folder (e.g. data/cafes/<id>), "
            "not the shared sample 'data/' root — keep each café's data separate."
        )
    created = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    menu = p / "menu.csv"
    if not menu.exists():
        menu.write_text(_MENU_TEMPLATE)
    return p, created


@dataclass
class Tenant:
    cafe_id: str
    name: str
    owner_phones: list[str] = field(default_factory=list)
    data_dir: str | None = None
    config_path: str | None = None
    db_path: str = ":memory:"

    def to_dict(self) -> dict:
        return {
            "cafe_id": self.cafe_id, "name": self.name,
            "owner_phones": self.owner_phones, "data_dir": self.data_dir,
            "config_path": self.config_path, "db_path": self.db_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        return cls(
            cafe_id=d["cafe_id"], name=d.get("name", d["cafe_id"]),
            owner_phones=d.get("owner_phones", []),
            data_dir=d.get("data_dir"), config_path=d.get("config_path"),
            db_path=d.get("db_path", ":memory:"),
        )


class TenantRegistry:
    """Routes each owner's message to their café's agent, lazily built & cached."""

    def __init__(self, tenants: list[Tenant], client: anthropic.Anthropic | None = None,
                 path: str | Path | None = None) -> None:
        self._client = client
        self._path = Path(path) if path else None
        self._by_id: dict[str, Tenant] = {}
        self._owner_index: dict[str, str] = {}   # owner phone → cafe_id
        self._agents: dict[str, RevenueAgent] = {}
        for t in tenants:
            self._register(t)

    def _register(self, t: Tenant) -> None:
        self._assert_isolated(t)
        self._by_id[t.cafe_id] = t
        for phone in t.owner_phones:
            self._owner_index[normalize_phone(phone)] = t.cafe_id

    def _assert_isolated(self, t: Tenant) -> None:
        """Refuse to share one café's POS data or database with another.

        Sugar Rush for Sugar Rush, SIP for SIP — never the twain shall meet.
        A misconfiguration raises loudly instead of leaking data.
        """
        for other in self._by_id.values():
            if other.cafe_id == t.cafe_id:
                continue  # updating the same café is fine
            if t.data_dir and other.data_dir and t.data_dir == other.data_dir:
                raise ValueError(
                    f"Data isolation violation: café '{t.cafe_id}' and '{other.cafe_id}' "
                    f"both point at data_dir '{t.data_dir}'. Each café must have its own "
                    f"POS folder — one café's data is never shared with another."
                )
            if (t.db_path and t.db_path != ":memory:"
                    and t.db_path == other.db_path):
                raise ValueError(
                    f"Data isolation violation: café '{t.cafe_id}' and '{other.cafe_id}' "
                    f"both use db_path '{t.db_path}'. Each café needs its own database."
                )

    # ── lookup / routing ──

    def tenant_for_owner(self, phone: str) -> Tenant | None:
        cafe_id = self._owner_index.get(normalize_phone(phone))
        return self._by_id.get(cafe_id) if cafe_id else None

    def agent_for(self, cafe_id: str) -> RevenueAgent:
        if cafe_id not in self._agents:
            t = self._by_id[cafe_id]
            self._agents[cafe_id] = RevenueAgent(
                store=Store(t.db_path),
                config=RevenueConfig.load(t.config_path),
                client=self._client,
                data_dir=t.data_dir,
            )
        return self._agents[cafe_id]

    def handle(self, from_phone: str, text: str) -> RevenueReply:
        """Route an inbound owner message to their café's agent."""
        tenant = self.tenant_for_owner(from_phone)
        if tenant is None:
            return RevenueReply(
                text="👋 Hi! This is the AsaanPay Revenue Advisor. I don't recognise "
                     "this number yet — ask your AsaanPay rep to connect your café and "
                     "I'll start advising you on how to grow revenue.",
                intent="unknown", action="unregistered",
            )
        return self.agent_for(tenant.cafe_id).handle_message(from_phone, text)

    def all_tenants(self) -> list[Tenant]:
        return list(self._by_id.values())

    def generate_digests(self, cadence: str) -> list[RevenueReply]:
        """Build due digests across every café (for the scheduler)."""
        out: list[RevenueReply] = []
        for cafe_id in self._by_id:
            out.extend(self.agent_for(cafe_id).generate_due_digests(cadence))
        return out

    # ── onboarding / persistence ──

    def add_cafe(self, tenant: Tenant, persist: bool = True) -> None:
        self._register(tenant)
        self._agents.pop(tenant.cafe_id, None)  # rebuild on next use
        if persist and self._path:
            self.save()

    def save(self, path: str | Path | None = None) -> None:
        target = Path(path) if path else self._path
        if target is None:
            raise ValueError("no path to save the tenant registry to")
        target.write_text(json.dumps(
            {"tenants": [t.to_dict() for t in self._by_id.values()]}, indent=2
        ))

    @classmethod
    def load(cls, path: str | Path,
             client: anthropic.Anthropic | None = None) -> "TenantRegistry":
        p = Path(path)
        raw = json.loads(p.read_text()) if p.exists() else {"tenants": []}
        tenants = [Tenant.from_dict(d) for d in raw.get("tenants", [])]
        return cls(tenants, client=client, path=p)
