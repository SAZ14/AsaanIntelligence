"""Conversational brain for the WhatsApp integrity agent.

Pure text-in / text-out so it can be driven by the Twilio webhook in production
and by plain function calls in tests. It resolves which venue the owner is
asking about, keeps a short-lived cache of each venue's audit, answers quick
commands deterministically (no LLM cost), and routes anything else to the
agent's grounded Q&A.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app import venues as venue_registry
from app.agents.integrity_agent import IntegrityAgentReport, answer_question, run_integrity_agent
from app.pos import RestaurantConfig, build_connector

CACHE_TTL_SECONDS = 900  # re-pull a venue's POS data at most every 15 min

HELP_TEXT = (
    "👋 I'm your venue integrity agent. Ask me anything, or use:\n"
    "• *summary* – headline audit\n"
    "• *revenue* – sales & payment mix\n"
    "• *profit* – profit & margin\n"
    "• *leakage* – suspected loss\n"
    "• *findings* – top issues to act on\n"
    "• *refresh* – re-pull latest POS data\n"
    "Or just ask, e.g. \"who is my worst staff member?\""
)


def _money(v: float) -> str:
    return f"PKR {v:,.0f}"


@dataclass
class _Cached:
    at: float
    report: IntegrityAgentReport


class IntegrityWhatsAppService:
    def __init__(
        self,
        restaurants: dict[str, RestaurantConfig] | None = None,
        owner_map: dict[str, str] | None = None,
        default_venue: str | None = "__use_registry__",
        llm_client=None,
        cache_ttl: int = CACHE_TTL_SECONDS,
    ) -> None:
        self.restaurants = restaurants if restaurants is not None else venue_registry.RESTAURANTS
        self.owner_map = owner_map if owner_map is not None else venue_registry.OWNER_WHATSAPP
        self.default_venue = (
            venue_registry.DEFAULT_VENUE if default_venue == "__use_registry__" else default_venue
        )
        self.llm_client = llm_client
        self.cache_ttl = cache_ttl
        self._cache: dict[str, _Cached] = {}

    # ── Venue resolution ──

    def resolve_venue(self, from_number: str) -> str | None:
        key = (from_number or "").strip()
        if key in self.owner_map:
            return self.owner_map[key]
        # tolerate a missing "whatsapp:" prefix
        if key.startswith("whatsapp:") and key[len("whatsapp:"):] in self.owner_map:
            return self.owner_map[key[len("whatsapp:"):]]
        return self.default_venue

    # ── Report cache ──

    def get_report(self, venue_key: str, force: bool = False) -> IntegrityAgentReport:
        cached = self._cache.get(venue_key)
        if not force and cached and (time.time() - cached.at) < self.cache_ttl:
            return cached.report
        config = self.restaurants[venue_key]
        data = build_connector(config).fetch()
        # Deterministic build: instant, free, exact. The LLM is used only for
        # free-form questions, on demand.
        report = run_integrity_agent(
            data.orders, data.menu, data.staff,
            venue_name=config.venue_name, use_llm=False,
        )
        self._cache[venue_key] = _Cached(at=time.time(), report=report)
        return report

    # ── Command formatters ──

    def _fmt_summary(self, r: IntegrityAgentReport) -> str:
        lines = [f"📋 *{r.venue_name}* — {r.period_days}-day audit", r.executive_summary]
        if r.findings:
            top = r.findings[0]
            lines.append(
                f"\nTop issue: {top.category.replace('_', ' ')} — {top.subject} "
                f"({_money(top.monetary_impact)})"
            )
        return "\n".join(lines)

    def _fmt_revenue(self, r: IntegrityAgentReport) -> str:
        rec = r.reconciliation
        lines = [
            f"💰 *Revenue — {r.venue_name}* ({r.period_days}d)",
            f"Collected: {_money(rec.gross_collected)} (incl. tax {_money(rec.tax_collected)})",
            f"Net sales: {_money(rec.net_sales)}",
            f"Avg/day: {_money(rec.gross_collected / max(rec.period_days, 1))}",
            "By payment method:",
        ]
        for mb in rec.by_method:
            lines.append(f"  {mb.method}: {_money(mb.gross_collected)} ({mb.share_pct:.0%})")
        return "\n".join(lines)

    def _fmt_profit(self, r: IntegrityAgentReport) -> str:
        rec = r.reconciliation
        return "\n".join([
            f"📈 *Profit — {r.venue_name}* ({r.period_days}d)",
            f"Net sales: {_money(rec.net_sales)}",
            f"COGS (sold): {_money(rec.cogs_sold)}",
            f"Gross profit: {_money(rec.gross_profit)} ({rec.gross_margin:.0%} margin)",
            f"Wasted COGS: {_money(rec.wasted_cogs)} (comps / fired-then-voided)",
        ])

    def _fmt_leakage(self, r: IntegrityAgentReport) -> str:
        integ = r.integrity
        lines = [
            f"🩸 *Leakage — {r.venue_name}*",
            f"Estimated: {_money(integ.estimated_leakage_period)} this period "
            f"(~{_money(integ.estimated_leakage_monthly)}/mo)",
            f"  • theft voids: {_money(integ.suspected_theft_value)}",
            f"  • excess comps: {_money(integ.excess_comp_value)}",
            f"  • excess discounts: {_money(integ.excess_discount_value)}",
        ]
        if integ.worst_offender:
            worst = next((s for s in integ.staff_integrity if s.staff_id == integ.worst_offender), None)
            if worst:
                lines.append(f"Worst offender: {worst.staff_name} ({worst.staff_id}), "
                             f"integrity score {worst.integrity_score:.0f}/100")
        rec = r.reconciliation
        if not rec.books_balanced:
            lines.append(f"⚠️ {rec.payment_mismatch_count} payment mismatch(es), "
                         f"{rec.tax_anomaly_count} tax anomaly(ies)")
        return "\n".join(lines)

    def _fmt_findings(self, r: IntegrityAgentReport, n: int = 5) -> str:
        if not r.findings:
            return f"✅ *{r.venue_name}*: no issues — clean books."
        lines = [f"🔎 *Top findings — {r.venue_name}*"]
        for f in r.findings[:n]:
            line = (f"{f.rank}. [{f.severity}] {f.category.replace('_', ' ')} — "
                    f"{f.subject}: {_money(f.monetary_impact)}")
            lines.append(line)
            if f.recommended_action:
                lines.append(f"   → {f.recommended_action}")
        return "\n".join(lines)

    # ── Main entry point ──

    def handle_message(self, from_number: str, body: str) -> str:
        venue_key = self.resolve_venue(from_number)
        if venue_key is None or venue_key not in self.restaurants:
            return ("This number isn't linked to a venue yet. "
                    "Ask your administrator to register it.")

        text = (body or "").strip()
        if not text:
            return HELP_TEXT
        cmd = text.lower().split()[0]

        try:
            if cmd in ("help", "start", "hi", "hello", "menu", "commands"):
                return HELP_TEXT
            if cmd in ("refresh", "reload", "update"):
                self.get_report(venue_key, force=True)
                return "🔄 Re-pulled the latest POS data. Ask away."

            report = self.get_report(venue_key)

            if cmd in ("summary", "report", "audit", "overview"):
                return self._fmt_summary(report)
            if cmd in ("revenue", "sales", "turnover"):
                return self._fmt_revenue(report)
            if cmd in ("profit", "margin", "profits"):
                return self._fmt_profit(report)
            if cmd in ("leakage", "leak", "loss", "losses", "theft"):
                return self._fmt_leakage(report)
            if cmd in ("findings", "issues", "top", "flags"):
                return self._fmt_findings(report)

            # Anything else: grounded free-form Q&A via the agent.
            return answer_question(report, text, client=self.llm_client)
        except FileNotFoundError:
            return "I couldn't reach this venue's POS data right now. Try again shortly."
        except Exception:
            return "Something went wrong handling that. Try *summary* or *help*."
