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
from app.analysis.periodic import (
    PeriodReport,
    build_daily_report,
    build_weekly_report,
)
from app.pos import RestaurantConfig, build_connector

CACHE_TTL_SECONDS = 900  # re-pull a venue's POS data at most every 15 min

HELP_TEXT = (
    "👋 I'm your venue integrity agent. Ask me anything, or use:\n"
    "• *summary* – headline audit\n"
    "• *revenue* – sales & payment mix\n"
    "• *profit* – profit & margin\n"
    "• *leakage* – suspected loss\n"
    "• *findings* – top issues to act on\n"
    "• *staff* – team integrity scores\n"
    "• *staff <name>* – drill into one person\n"
    "• *daily* – yesterday's report\n"
    "• *weekly* – the week, summarised\n"
    "• *report* – full PDF audit\n"
    "• *refresh* – re-pull latest POS data\n"
    "Or just ask, e.g. \"who is my worst staff member?\""
)


def _money(v: float) -> str:
    return f"PKR {v:,.0f}"


@dataclass
class _Cached:
    at: float
    report: IntegrityAgentReport


@dataclass
class _CachedData:
    at: float
    data: object  # POSData (orders / menu / staff)


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
        self._data_cache: dict[str, _CachedData] = {}

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

    def get_data(self, venue_key: str, force: bool = False):
        """Fetch (and cache) a venue's raw POS data — shared by every report."""
        cached = self._data_cache.get(venue_key)
        if not force and cached and (time.time() - cached.at) < self.cache_ttl:
            return cached.data
        config = self.restaurants[venue_key]
        data = build_connector(config).fetch()
        self._data_cache[venue_key] = _CachedData(at=time.time(), data=data)
        return data

    def get_report(self, venue_key: str, force: bool = False) -> IntegrityAgentReport:
        cached = self._cache.get(venue_key)
        if not force and cached and (time.time() - cached.at) < self.cache_ttl:
            return cached.report
        config = self.restaurants[venue_key]
        data = self.get_data(venue_key, force=force)
        # Deterministic build: instant, free, exact. The LLM is used only for
        # free-form questions, on demand.
        report = run_integrity_agent(
            data.orders, data.menu, data.staff,
            venue_name=config.venue_name, use_llm=False,
        )
        self._cache[venue_key] = _Cached(at=time.time(), report=report)
        return report

    # ── Period digests (daily / weekly) ──

    def daily_report(self, venue_key: str, force: bool = False) -> PeriodReport | None:
        config = self.restaurants[venue_key]
        data = self.get_data(venue_key, force=force)
        return build_daily_report(
            data.orders, data.menu, data.staff, venue_name=config.venue_name
        )

    def weekly_report(self, venue_key: str, force: bool = False) -> PeriodReport | None:
        config = self.restaurants[venue_key]
        data = self.get_data(venue_key, force=force)
        return build_weekly_report(
            data.orders, data.menu, data.staff, venue_name=config.venue_name
        )

    def daily_digest(self, venue_key: str, force: bool = False) -> str:
        period = self.daily_report(venue_key, force=force)
        return self._fmt_daily(period) if period else "No POS data yet for a daily report."

    def weekly_digest(self, venue_key: str, force: bool = False) -> str:
        period = self.weekly_report(venue_key, force=force)
        return self._fmt_weekly(period) if period else "No POS data yet for a weekly report."

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

    def _fmt_staff_list(self, r: IntegrityAgentReport) -> str:
        roster = sorted(r.integrity.staff_integrity, key=lambda s: s.integrity_score)
        lines = [f"👥 *Team integrity — {r.venue_name}* (worst → best)"]
        for s in roster:
            flag = "🔴" if s.integrity_score < 90 else ("🟡" if s.integrity_score < 99 else "🟢")
            leak = f" · leak {_money(s.total_leakage)}" if s.total_leakage > 0 else ""
            lines.append(f"{flag} {s.staff_name} ({s.staff_id}): {s.integrity_score:.0f}/100{leak}")
        lines.append("\nReply *staff <name>* for one person's detail.")
        return "\n".join(lines)

    def _fmt_staff_detail(self, r: IntegrityAgentReport, query: str) -> str:
        q = query.strip().lower()
        match = None
        for s in r.integrity.staff_integrity:
            if q == s.staff_id.lower() or q in s.staff_name.lower():
                match = s
                break
        if match is None:
            names = ", ".join(s.staff_name for s in r.integrity.staff_integrity)
            return f"No staff matching “{query}”. Try one of: {names}."

        bl = r.integrity.venue_baseline
        events = [e for e in r.integrity.flagged_events if e.staff_id == match.staff_id]
        lines = [
            f"👤 *{match.staff_name} ({match.staff_id})*",
            f"Integrity score: {match.integrity_score:.0f}/100",
            f"Lines rung: {match.total_lines} · gross {_money(match.gross_volume)} · cash {match.cash_share:.0%}",
            f"Void rate: {match.void_rate:.1%} (venue {bl.void_rate:.1%}, z={match.void_rate_z:.1f})",
            f"Comp rate: {match.comp_rate:.1%} (venue {bl.comp_rate:.1%}, z={match.comp_rate_z:.1f})",
            f"Discount rate: {match.discount_rate:.1%} (venue {bl.discount_rate:.1%}, z={match.discount_rate_z:.1f})",
            "Excess (vs baseline):",
            f"  • theft voids: {_money(match.excess_theft_void_value)}",
            f"  • comps: {_money(match.excess_comp_value)}",
            f"  • discounts: {_money(match.excess_discount_value)}",
            f"Total leakage: {_money(match.total_leakage)}",
        ]
        if events:
            lines.append(f"\nTop flagged events ({len(events)}):")
            for e in events[:3]:
                lines.append(f"  {e.order_id} · {e.flag_type.replace('_', ' ')} · "
                             f"{e.item_name} · {_money(e.value)}")
        return "\n".join(lines)

    # ── Period digest formatters ──

    @staticmethod
    def _delta(curr: float, prev: float, good_up: bool = True) -> str:
        """Human direction tag, e.g. '🟢 ↑ 12%' or '🔴 ↑ 30%' for leakage."""
        if prev <= 0:
            return "(new)" if curr > 0 else ""
        pct = (curr - prev) / prev * 100.0
        if abs(pct) < 1:
            return "→ flat"
        arrow = "↑" if pct > 0 else "↓"
        favourable = (pct > 0) == good_up
        dot = "🟢" if favourable else "🔴"
        return f"{dot} {arrow} {abs(pct):.0f}%"

    def _fmt_daily(self, p: PeriodReport) -> str:
        rec = p.report.reconciliation
        integ = p.report.integrity
        prev = p.previous
        lines = [
            f"☀️ *Daily report — {p.venue_name}*",
            f"{p.label}",
            "",
            f"Net sales: {_money(rec.net_sales)}  "
            f"{self._delta(p.current.net_sales, prev.net_sales) if prev else ''}".rstrip(),
            f"Orders: {rec.total_orders}"
            + (f"  {self._delta(p.current.orders, prev.orders)}" if prev else ""),
            f"Gross profit: {_money(rec.gross_profit)} ({rec.gross_margin:.0%})",
        ]
        if integ.estimated_leakage_period > 0:
            tag = self._delta(p.current.leakage, prev.leakage, good_up=False) if prev else ""
            lines.append(f"Leakage today: {_money(integ.estimated_leakage_period)}  {tag}".rstrip())
            if p.report.findings:
                top = p.report.findings[0]
                lines.append(
                    f"⚠️ {top.category.replace('_', ' ')} — {top.subject} "
                    f"({_money(top.monetary_impact)})"
                )
        else:
            lines.append("✅ No leakage flagged today.")
        if not rec.books_balanced:
            lines.append(f"⚠️ {rec.payment_mismatch_count} payment mismatch(es) to review.")
        return "\n".join(lines)

    def _fmt_weekly(self, p: PeriodReport) -> str:
        rec = p.report.reconciliation
        integ = p.report.integrity
        prev = p.previous
        days = max((p.end - p.start).days + 1, 1)
        lines = [
            f"📅 *Weekly summary — {p.venue_name}*",
            f"{p.label}",
            "",
            f"Net sales: {_money(rec.net_sales)}  "
            f"{self._delta(p.current.net_sales, prev.net_sales) if prev else ''}".rstrip(),
            f"Avg/day: {_money(rec.net_sales / days)} · {rec.total_orders} orders",
            f"Gross profit: {_money(rec.gross_profit)} ({rec.gross_margin:.0%})",
            "",
            f"🩸 Leakage this week: {_money(integ.estimated_leakage_period)}  "
            f"{self._delta(p.current.leakage, prev.leakage, good_up=False) if prev else ''}".rstrip(),
            f"  • theft voids: {_money(integ.suspected_theft_value)}",
            f"  • excess comps: {_money(integ.excess_comp_value)}",
            f"  • excess discounts: {_money(integ.excess_discount_value)}",
        ]
        if integ.worst_offender:
            worst = next((s for s in integ.staff_integrity if s.staff_id == integ.worst_offender), None)
            if worst and worst.total_leakage > 0:
                lines.append(
                    f"Worst offender: {worst.staff_name} ({worst.staff_id}), "
                    f"{worst.integrity_score:.0f}/100"
                )
        if p.days:
            best = max(p.days, key=lambda d: d.net_sales)
            worst_day = min((d for d in p.days if d.orders > 0), key=lambda d: d.net_sales, default=None)
            lines.append("")
            lines.append(f"Best day: {best.day:%a} {_money(best.net_sales)}")
            if worst_day and worst_day.day != best.day:
                lines.append(f"Slowest: {worst_day.day:%a} {_money(worst_day.net_sales)}")
        if p.report.findings:
            lines.append("\nTop issues to act on:")
            for f in p.report.findings[:3]:
                lines.append(
                    f"{f.rank}. {f.category.replace('_', ' ')} — {f.subject}: "
                    f"{_money(f.monetary_impact)}"
                )
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
            if cmd in ("report", "pdf", "document"):
                # The webhook attaches the generated PDF as media.
                return f"📄 Here's your full PDF audit for {self.restaurants[venue_key].venue_name}."

            if cmd in ("daily", "today", "yesterday", "day"):
                return self.daily_digest(venue_key)
            if cmd in ("weekly", "week"):
                return self.weekly_digest(venue_key)

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
            if cmd in ("staff", "team", "employee", "employees", "waiter", "server"):
                rest = text[len(text.split()[0]):].strip()
                return self._fmt_staff_detail(report, rest) if rest else self._fmt_staff_list(report)

            # Anything else: grounded free-form Q&A via the agent.
            return answer_question(report, text, client=self.llm_client)
        except FileNotFoundError:
            return "I couldn't reach this venue's POS data right now. Try again shortly."
        except Exception:
            return "Something went wrong handling that. Try *summary* or *help*."
