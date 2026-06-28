"""Integrity agent service — per-store cache, command routing, DB-driven POS config."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from app.agents.integrity.pos.base import RestaurantConfig, build_connector
# Import connectors so they self-register via register_connector()
import app.agents.integrity.pos.csv_connector  # noqa: F401
import app.agents.integrity.pos.rest_connector  # noqa: F401
from app.agents.integrity.agents.integrity_agent import (
    IntegrityAgentReport, run_integrity_agent, answer_question,
)

CACHE_TTL = 900  # 15 min

HELP_TEXT = (
    "Integrity Agent\n\n"
    "summary     — executive summary\n"
    "leakage     — leakage breakdown\n"
    "profit      — profit & COGS\n"
    "staff       — per-staff anomalies\n"
    "daily       — today's period report\n"
    "weekly      — 7-day trend\n"
    "refresh     — re-pull POS data\n"
    "pdf         — full PDF audit (link)\n"
    "Or ask any question about your POS data."
)


@dataclass
class _Cached:
    at: float
    report: IntegrityAgentReport


class IntegrityService:
    def __init__(self) -> None:
        self._cache: dict[int, _Cached] = {}
        self._data_cache: dict[int, object] = {}
        self._data_ts: dict[int, float] = {}

    def _get_config(self, store_id: int) -> RestaurantConfig | None:
        from app.core.db import SessionLocal, POSConnection, Store
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            pos = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
            if not store or not pos:
                return None
            return RestaurantConfig(
                venue_name=store.name,
                pos_type=pos.pos_type,
                connection=dict(pos.config or {}),
                mapping=pos.mapping or "cafe_generic",
                currency=pos.currency or "PKR",
                timezone=pos.timezone or "Asia/Karachi",
            )

    def get_data(self, store_id: int, force: bool = False):
        now = time.time()
        if not force and store_id in self._data_cache and (now - self._data_ts.get(store_id, 0)) < CACHE_TTL:
            return self._data_cache[store_id]
        config = self._get_config(store_id)
        if config is None:
            raise FileNotFoundError(f"No POS connection for store {store_id}")
        data = build_connector(config).fetch()
        self._data_cache[store_id] = data
        self._data_ts[store_id] = now
        return data

    def get_report(self, store_id: int, force: bool = False) -> IntegrityAgentReport:
        now = time.time()
        cached = self._cache.get(store_id)
        if not force and cached and (now - cached.at) < CACHE_TTL:
            return cached.report
        config = self._get_config(store_id)
        if config is None:
            raise FileNotFoundError(f"No POS connection for store {store_id}")
        data = self.get_data(store_id, force=force)
        report = run_integrity_agent(
            data.orders, data.menu, data.staff,
            venue_name=config.venue_name, use_llm=False,
        )
        self._cache[store_id] = _Cached(at=now, report=report)
        return report

    def handle_message(self, store_id: int, from_phone: str, body: str) -> str:
        text = (body or "").strip()
        if not text or text.lower() in ("help", "start", "hi", "hello", "commands"):
            return HELP_TEXT
        cmd = text.lower().split()[0]
        try:
            if cmd in ("refresh", "reload", "update"):
                self.get_report(store_id, force=True)
                return "Re-pulled the latest POS data. Ask away."

            if cmd in ("report", "pdf", "document"):
                config = self._get_config(store_id)
                name = config.venue_name if config else "this restaurant"
                return f"Generating PDF audit for {name}. Request via /report/{store_id}.pdf"

            report = self.get_report(store_id)

            if cmd in ("summary", "overview"):
                return report.executive_summary or _fallback_summary(report)

            if cmd in ("leakage", "leak", "theft", "fraud"):
                return _leakage_text(report)

            if cmd in ("profit", "margin", "cogs", "revenue", "sales"):
                return _profit_text(report)

            if cmd in ("staff", "employees", "team"):
                return _staff_text(report)

            if cmd == "daily":
                return _daily_report(store_id, report)

            if cmd == "weekly":
                return _weekly_report(store_id, report)

            # Free-form question — use LLM if available
            answer = answer_question(report, text)
            return answer or HELP_TEXT

        except FileNotFoundError:
            return "POS not configured. Ask admin: POST /admin/stores/{id}/pos"
        except Exception as exc:
            logger.exception("Integrity agent error store=%d: %s", store_id, exc)
            return "Something went wrong. Try 'summary' or 'help'."


def _fallback_summary(r: IntegrityAgentReport) -> str:
    rec = r.reconciliation
    integ = r.integrity
    return (
        f"{r.venue_name} — {r.period_days}d: "
        f"Net sales PKR {rec.net_sales:,.0f} | "
        f"Gross profit PKR {rec.gross_profit:,.0f} ({rec.gross_margin:.0%}) | "
        f"Leakage PKR {integ.estimated_leakage_period:,.0f} "
        f"(~{integ.estimated_leakage_monthly:,.0f}/mo)"
    )


def _leakage_text(r: IntegrityAgentReport) -> str:
    integ = r.integrity
    lines = [
        f"Estimated leakage: PKR {integ.estimated_leakage_period:,.0f} "
        f"(monthly ~PKR {integ.estimated_leakage_monthly:,.0f})",
        f"  Theft voids: PKR {integ.suspected_theft_value:,.0f}",
        f"  Excess comps: PKR {integ.excess_comp_value:,.0f}",
        f"  Excess discounts: PKR {integ.excess_discount_value:,.0f}",
    ]
    for f in r.findings[:3]:
        lines.append(f"  [{f.severity}] {f.subject}: PKR {f.monetary_impact:,.0f}")
    return "\n".join(lines)


def _profit_text(r: IntegrityAgentReport) -> str:
    rec = r.reconciliation
    return (
        f"Net sales: PKR {rec.net_sales:,.0f}\n"
        f"Gross profit: PKR {rec.gross_profit:,.0f} ({rec.gross_margin:.1%})\n"
        f"COGS sold: PKR {rec.cogs_sold:,.0f}\n"
        f"Wasted COGS (comps/voids): PKR {rec.wasted_cogs:,.0f}\n"
        f"Payment mismatches: {rec.payment_mismatch_count} "
        f"(PKR {rec.net_unreconciled:,.0f} unreconciled)"
    )


def _staff_text(r: IntegrityAgentReport) -> str:
    staff_findings = [f for f in r.findings if f.category in ("theft", "comp_abuse", "discount_abuse")]
    if not staff_findings:
        return "No significant staff anomalies detected."
    lines = ["Staff anomalies (impact-ranked):"]
    for f in staff_findings[:5]:
        lines.append(
            f"  [{f.severity}] {f.subject} — {f.category}: "
            f"PKR {f.monetary_impact:,.0f}\n  {f.evidence}"
        )
    return "\n".join(lines)


def _fmt_period(rpt) -> str:
    """Format a PeriodReport into a WhatsApp-friendly string."""
    if rpt is None:
        return "No data available for the requested period."
    cur = rpt.current
    prev = rpt.previous
    lines = [f"{rpt.label} — {rpt.venue_name}"]
    lines.append(f"Orders: {cur.orders}  |  Sales: PKR {cur.net_sales:,.0f}")
    lines.append(f"Profit: PKR {cur.gross_profit:,.0f} ({cur.gross_margin:.0%})")
    lines.append(f"Leakage: PKR {cur.leakage:,.0f}")
    if prev and prev.has_data and prev.net_sales > 0:
        delta = (cur.net_sales - prev.net_sales) / prev.net_sales
        arrow = "▲" if delta >= 0 else "▼"
        lines.append(f"vs prev: {arrow} {abs(delta):.0%} sales")
    if rpt.kind == "weekly" and rpt.days:
        lines.append("")
        lines.append("Daily breakdown:")
        for pt in rpt.days:
            lines.append(
                f"  {pt.day.strftime('%a %d')}: PKR {pt.net_sales:,.0f} "
                f"({pt.orders} orders, leak PKR {pt.leakage:,.0f})"
            )
    return "\n".join(lines)


def _daily_report(store_id: int, base: IntegrityAgentReport) -> str:
    try:
        from app.agents.integrity.analysis.periodic import build_daily_report
        data = get_service().get_data(store_id)
        rpt = build_daily_report(data.orders, data.menu, data.staff, venue_name=base.venue_name)
        return _fmt_period(rpt)
    except Exception:
        return _fallback_summary(base) + "\n(Full daily data unavailable)"


def _weekly_report(store_id: int, base: IntegrityAgentReport) -> str:
    try:
        from app.agents.integrity.analysis.periodic import build_weekly_report
        data = get_service().get_data(store_id)
        rpt = build_weekly_report(data.orders, data.menu, data.staff, venue_name=base.venue_name)
        return _fmt_period(rpt)
    except Exception:
        return _fallback_summary(base) + "\n(Full weekly data unavailable)"


# Module-level singleton
_service: IntegrityService | None = None


def get_service() -> IntegrityService:
    global _service
    if _service is None:
        _service = IntegrityService()
    return _service
