"""The Revenue advisor engine.

Claude (in :mod:`nlu`) parses the owner's question; *this* module computes every
number deterministically and composes the WhatsApp reply. Also builds the
scheduled daily/weekly/monthly digests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import anthropic

from app.models.canonical import MenuItem, Order, Staff
from app.agents.revenue.analytics import (
    RevenueDigest,
    build_digest,
    detect_dead_windows,
    product_performance,
    recommend_campaigns,
)
from app.agents.revenue.config import RevenueConfig
from app.agents.revenue.datasource import (
    PERIOD_DAYS,
    filter_period,
    load_pos,
    normalize_period,
    period_label,
)
from app.agents.revenue.models import CampaignLogEntry, OwnerSubscription
from app.agents.revenue.nlu import parse_query
from app.agents.revenue.pricing import simple_price_moves
from app.agents.revenue.segments import build_segments
from app.agents.revenue.store import Store
from app.agents.revenue.strategy import (
    analyze_attach,
    build_playbook,
    frequency_opportunities,
    menu_opportunities,
)


@dataclass
class RevenueReply:
    text: str
    intent: str = "unknown"
    period: str = "week"
    action: str = "answer"
    campaigns_logged: int = 0
    outbound: list[tuple[str, str]] = field(default_factory=list)


def _money(x: float) -> str:
    return f"PKR {x:,.0f}"


class RevenueAgent:
    def __init__(
        self,
        store: Store | None = None,
        orders: list[Order] | None = None,
        menu: dict[str, MenuItem] | None = None,
        staff: dict[str, Staff] | None = None,
        config: RevenueConfig | None = None,
        client: anthropic.Anthropic | None = None,
        as_of: date | None = None,
        data_dir=None,
    ) -> None:
        self.store = store or Store(":memory:")
        self.config = config or RevenueConfig()
        self.client = client
        if orders is None:
            orders, menu, staff = load_pos(data_dir)
        self.orders = orders
        self.menu = menu or {}
        self.staff = staff or {}
        self.as_of = as_of
        self._price_wins: str | None = None
        self._bigger_moves: str | None = None

    # ── always-on advice (simple price wins first, then bigger plays) ──

    def _price_wins_block(self, n: int = 3) -> str:
        """The simplest, most concrete advice: small price bumps on big sellers."""
        if self._price_wins is None:
            orders, _p, _l = self._window("month")
            moves = simple_price_moves(orders, self.menu, self.config, period_days=30)
            ups = [m for m in moves if m.direction == "up"][:n]
            if not ups:
                self._price_wins = ""
            else:
                lines = ["\n💰 Quick price wins (sell well → small bump):"]
                for m in ups:
                    lines.append(
                        f"• {m.name} sells ~{m.units_month}/mo — raise PKR {m.bump:.0f} "
                        f"({_money(m.current_price)}→{_money(m.new_price)}) = "
                        f"+{_money(m.monthly_impact)}/mo"
                    )
                self._price_wins = "\n".join(lines)
        return self._price_wins

    def _bigger_moves_block(self, n: int = 2) -> str:
        """The larger growth levers, after the simple price advice."""
        if self._bigger_moves is None:
            orders, _p, _l = self._window("month")
            pb = build_playbook(orders, self.menu, self.staff, self.config, period_days=30)
            if not pb.items:
                self._bigger_moves = ""
            else:
                lines = ["\n📈 Then bigger plays:"]
                for i, it in enumerate(pb.items[:n], 1):
                    impact = f" (~{_money(it.est_monthly_impact)}/mo)" if it.est_monthly_impact else ""
                    first_action = it.action.split(".")[0].strip()
                    lines.append(f"{i}. [{it.lever}] {it.title}{impact} — {first_action}.")
                lines.append("Say 'how do I grow revenue' for the full plan.")
                self._bigger_moves = "\n".join(lines)
        return self._bigger_moves

    def _advise(self, text: str) -> str:
        """Append simple price advice first, then the bigger growth plays."""
        return text + self._price_wins_block() + self._bigger_moves_block()

    # ── entry point ──

    def handle_message(self, phone: str, text: str) -> RevenueReply:
        q = parse_query(text, client=self.client)

        if q.intent == "summary":
            return self._summary(q.period)
        if q.intent == "best_sellers":
            return self._best_sellers(q.period)
        if q.intent == "pricing":
            return self._pricing(q.period)
        if q.intent == "dead_windows":
            return self._dead_windows(q.period)
        if q.intent == "campaigns":
            return self._campaigns(q.period)
        if q.intent == "strategy":
            return self._strategy(q.period)
        if q.intent == "upsell":
            return self._upsell(q.period)
        if q.intent == "menu":
            return self._menu(q.period)
        if q.intent == "loyalty":
            return self._loyalty(q.period)
        if q.intent == "subscribe":
            return self._subscribe(phone, q.cadence or "weekly")
        if q.intent == "help":
            return self._help()
        return self._help(unknown=True)

    # ── handlers ──

    def _window(self, period: str):
        period = normalize_period(period)
        orders, start, end = filter_period(self.orders, period, self.as_of)
        return orders, period, period_label(period, start, end)

    def _summary(self, period: str) -> RevenueReply:
        orders, period, label = self._window(period)
        perf = product_performance(orders, self.menu, self.staff, period_label=label)
        if not orders:
            return RevenueReply(text=f"No sales found for {label}.", intent="summary", period=period)
        top = perf.top_sellers[0] if perf.top_sellers else None
        top_line = f" Best seller: {top.name} ({top.units} sold)." if top else ""
        text = (
            f"{self.config.venue_name} — {label}\n"
            f"Revenue: {_money(perf.total_revenue)} across {perf.order_count} orders "
            f"(avg ticket {_money(perf.avg_ticket)}).{top_line}"
        )
        return RevenueReply(text=self._advise(text), intent="summary", period=period)

    def _best_sellers(self, period: str) -> RevenueReply:
        orders, period, label = self._window(period)
        perf = product_performance(orders, self.menu, self.staff, period_label=label)
        if not perf.top_sellers:
            return RevenueReply(text=f"No sales found for {label}.", intent="best_sellers", period=period)
        lines = [f"Top sellers — {label}:"]
        for i, s in enumerate(perf.top_sellers, 1):
            lines.append(f"{i}. {s.name} — {s.units} sold, {_money(s.revenue)}")
        biggest = perf.top_margin[0] if perf.top_margin else None
        if biggest:
            lines.append(f"Biggest profit driver: {biggest.name} "
                         f"({_money(biggest.margin_contribution)} margin).")
        return RevenueReply(text=self._advise("\n".join(lines)),
                            intent="best_sellers", period=period)

    def _pricing(self, period: str) -> RevenueReply:
        # Pricing signals are more reliable over a longer window.
        period = "month" if period == "day" else period
        orders, period, label = self._window(period)
        moves = simple_price_moves(
            orders, self.menu, self.config, period_days=PERIOD_DAYS.get(period, 30)
        )
        ups = [m for m in moves if m.direction == "up"]
        downs = [m for m in moves if m.direction == "down"]
        if not ups and not downs:
            return RevenueReply(
                text="No clear price changes stand out — demand signals don't show "
                     "pricing power right now.",
                intent="pricing", period=period,
            )
        lines = ["Price tweaks straight from your sales data:"]
        total = 0.0
        for m in ups:
            total += m.monthly_impact
            lines.append(
                f"• {m.name} sells ~{m.units_month}/mo — raise PKR {m.bump:.0f} "
                f"({_money(m.current_price)}→{_money(m.new_price)}) = "
                f"+{_money(m.monthly_impact)}/mo"
            )
        if ups:
            lines.append(f"It sells regardless, so volume holds → about "
                         f"{_money(total)}/mo extra from these small raises.")
        for m in downs:
            lines.append(f"• Consider lowering {m.name}: {m.reason} "
                         f"({_money(m.current_price)}→{_money(m.new_price)}).")
        # This reply *is* the price advice, so only add the bigger plays after it.
        return RevenueReply(text="\n".join(lines) + self._bigger_moves_block(),
                            intent="pricing", period=period)

    def _dead_windows(self, period: str) -> RevenueReply:
        orders, period, label = self._window("month")  # need enough samples
        windows = detect_dead_windows(orders)
        if not windows:
            return RevenueReply(text="Not enough data to spot quiet windows yet.",
                                intent="dead_windows", period=period)
        lines = ["Quietest windows (orders per occurrence):"]
        for w in windows:
            lines.append(f"• {w.day_name} {w.daypart} "
                         f"({w.start_hour:02d}:00–{w.end_hour:02d}:00): "
                         f"~{w.avg_orders:.0f} orders, {w.gap_vs_peak:.0f} below peak.")
        lines.append("Want me to suggest brand-safe campaigns to fill these? Say 'campaign ideas'.")
        return RevenueReply(text=self._advise("\n".join(lines)), intent="dead_windows", period=period)

    def _campaigns(self, period: str) -> RevenueReply:
        orders, period, label = self._window("month")
        windows = detect_dead_windows(orders)
        segments = build_segments(orders, self.menu, self.staff, self.config)
        perf = product_performance(orders, self.menu, self.staff)
        recs = recommend_campaigns(windows, segments, self.config, perf.avg_ticket)
        if not recs:
            return RevenueReply(text="No campaign opportunities surfaced from the data yet.",
                                intent="campaigns", period=period)
        lines = ["Brand-safe micro-campaigns to lift revenue:"]
        logged = 0
        for r in recs:
            lines.append(f"• {r.message}")
            self.store.log_campaign(CampaignLogEntry(
                window_desc=r.window_desc, campaign_name=r.campaign_name,
                target_segment=r.target_segment, audience_size=r.audience_size,
                expected_redemptions=r.expected_redemptions,
                est_added_revenue=r.est_added_revenue,
            ))
            logged += 1
        return RevenueReply(text="\n".join(lines), intent="campaigns", period=period,
                            action="recommend", campaigns_logged=logged)

    def _strategy(self, period: str) -> RevenueReply:
        orders, period, label = self._window("month")
        pb = build_playbook(orders, self.menu, self.staff, self.config, period_days=30)
        if not pb.items:
            return RevenueReply(text="Not enough data to build a growth plan yet.",
                                intent="strategy", period=period)
        lines = [f"How to grow revenue at {self.config.venue_name}:", pb.headline]
        for i, it in enumerate(pb.items, 1):
            impact = f" (~{_money(it.est_monthly_impact)}/mo)" if it.est_monthly_impact else ""
            lines.append(f"{i}. [{it.lever}] {it.title}{impact}\n   {it.action}")
        return RevenueReply(text="\n".join(lines), intent="strategy", period=period,
                            action="advise")

    def _upsell(self, period: str) -> RevenueReply:
        orders, period, label = self._window("month")
        a = analyze_attach(orders, self.menu, self.config, period_days=30)
        if not a.beverage_orders:
            return RevenueReply(text="No drink orders found to analyse for upsell.",
                                intent="upsell", period=period)
        bundle = f"\n• Bundle “{a.top_bundle}” as a set-price combo." if a.top_bundle else ""
        text = (
            "Raise the average ticket:\n"
            f"• Food attach is {a.attach_rate*100:.0f}% of drink orders "
            f"({a.beverage_orders} drink orders). Get baristas suggesting a pastry / "
            f"premium add-on (extra shot, syrup, dairy-free) → target {a.target_rate*100:.0f}%.\n"
            f"• Each point of attach ≈ PKR {a.avg_food_price:,.0f} per order added; "
            f"reaching target ≈ {_money(a.est_monthly_uplift)}/mo.{bundle}\n"
            "• Add a premium tier (coffee flight, single-origin) to nudge ticket up."
        )
        return RevenueReply(text=text, intent="upsell", period=period, action="advise")

    def _menu(self, period: str) -> RevenueReply:
        orders, period, label = self._window("month")
        moves = menu_opportunities(orders, self.menu, self.staff, self.config)
        if not moves:
            return RevenueReply(text="No clear menu moves surfaced yet.",
                                intent="menu", period=period)
        labels = {"feature": "Feature", "fix": "Fix/cut", "add": "Add"}
        lines = ["Menu optimisation:"]
        for m in moves:
            lines.append(f"• {labels.get(m.kind, m.kind)} {m.name} — {m.detail}")
        return RevenueReply(text="\n".join(lines), intent="menu", period=period,
                            action="advise")

    def _loyalty(self, period: str) -> RevenueReply:
        orders, period, label = self._window("month")
        f = frequency_opportunities(orders, self.menu, self.staff, self.config)
        text = (
            "Drive repeat business:\n"
            f"• Repeat rate is {f.repeat_rate*100:.0f}% across {f.regulars} regulars. "
            f"{f.loyalty_note}\n"
            f"• {f.lapsed_regulars} regulars have gone quiet (≈ {_money(f.winback_value)} "
            "of lost value) — send them a members-only invite to win them back.\n"
            "• Host events in slow hours (open-mic, tastings) to build a habit of coming in."
        )
        return RevenueReply(text=text, intent="loyalty", period=period, action="advise")

    def _subscribe(self, phone: str, cadence: str) -> RevenueReply:
        cadence = cadence if cadence in ("daily", "weekly", "monthly") else "weekly"
        self.store.upsert_subscription(OwnerSubscription(phone=phone, cadence=cadence))
        return RevenueReply(
            text=f"Done — I'll send you a {cadence} revenue digest. "
                 "Reply 'stop digests' any time.",
            intent="subscribe", period=cadence_to_period(cadence), action="subscribed",
        )

    def _help(self, unknown: bool = False) -> RevenueReply:
        prefix = ("I didn't quite catch that. " if unknown else "")
        text = (
            f"{prefix}I'm your revenue advisor for {self.config.venue_name}. Ask me:\n"
            "• 'How do I grow revenue?' — full growth playbook\n"
            "• 'How did we do this week?' — revenue summary\n"
            "• 'Best sellers this month' — top products\n"
            "• 'How do I raise the average ticket?' — upsell & combos\n"
            "• 'Menu advice' — high-margin heroes, dogs, gaps\n"
            "• 'How do I get repeat customers?' — loyalty & win-back\n"
            "• 'What can I raise prices on?' — pricing advice\n"
            "• 'When are we slow?' — dead windows\n"
            "• 'Campaign ideas' — brand-safe ways to fill quiet times\n"
            "• 'Send me a weekly digest' — scheduled updates"
        )
        return RevenueReply(text=text, intent="help")

    # ── scheduled digest ──

    def build_owner_digest(self, period: str) -> RevenueDigest:
        orders, period, label = self._window(period)
        return build_digest(orders, self.menu, self.staff, label, self.config,
                            period_days=PERIOD_DAYS.get(period, 7))

    def generate_due_digests(self, cadence: str) -> list[RevenueReply]:
        """Build digests for every active subscriber on this cadence.

        Intended to be called by a scheduler (cron / send_later). Returns one
        reply per subscriber with the message queued in ``outbound``.
        """
        period = cadence_to_period(cadence)
        out: list[RevenueReply] = []
        for sub in self.store.active_subscriptions(cadence):
            digest = self.build_owner_digest(period)
            text = self._advise(render_digest_text(digest, self.config.venue_name))
            out.append(RevenueReply(
                text=text, intent="summary", period=period, action="digest",
                outbound=[(sub.phone, text)],
            ))
        return out


def cadence_to_period(cadence: str) -> str:
    return {"daily": "day", "weekly": "week", "monthly": "month"}.get(cadence, "week")


def render_digest_text(digest: RevenueDigest, venue_name: str) -> str:
    perf = digest.performance
    lines = [f"{venue_name} — revenue digest · {digest.period_label}",
             f"Revenue {_money(perf.total_revenue)} · {perf.order_count} orders · "
             f"avg ticket {_money(perf.avg_ticket)}"]
    if perf.top_sellers:
        tops = ", ".join(f"{s.name} ({s.units})" for s in perf.top_sellers[:3])
        lines.append(f"Top sellers: {tops}")
    if digest.pricing:
        r = digest.pricing[0]
        lines.append(f"Pricing tip: {r.name} {_money(r.current_price)}→{_money(r.suggested_price)} "
                     f"(~{_money(r.est_monthly_uplift)}/mo)")
    if digest.campaigns:
        lines.append(f"Campaign: {digest.campaigns[0].message}")
    if digest.identified_caveat:
        lines.append(f"Note: {digest.identified_caveat}")
    return "\n".join(lines)


# ── convenience one-shot runner (mirrors run_<name>_agent convention) ──

def run_revenue_agent(
    questions: list[str],
    orders: list[Order] | None = None,
    menu: dict[str, MenuItem] | None = None,
    staff: dict[str, Staff] | None = None,
    config: RevenueConfig | None = None,
    client: anthropic.Anthropic | None = None,
    as_of: date | None = None,
    phone: str = "+920000000000",
) -> list[RevenueReply]:
    """Run a list of owner questions through a fresh Revenue agent."""
    agent = RevenueAgent(
        orders=orders, menu=menu, staff=staff, config=config,
        client=client, as_of=as_of,
    )
    return [agent.handle_message(phone, q) for q in questions]

