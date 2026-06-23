from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from app.analysis.integrity import IntegrityReport, StaffIntegrity, FlaggedEvent
from app.analysis.retention import RetentionReport, OperationsReport, CustomerProfile

FLAGGED_SCORE_THRESHOLD = 99.0
WINNABLE_GAP_MAX_DAYS = 30
DEFAULT_RECOVERY_RATE = 0.30
SANITY_WINBACK_PCT_WARN = 0.10


@dataclass
class HeadlineNumbers:
    monthly_leakage: float = 0.0
    monthly_leakage_flagged_staff: list[StaffIntegrity] = field(default_factory=list)
    venue_wide_leakage_monthly: float = 0.0
    monthly_winback_tier_a: float = 0.0
    monthly_winback_total: float = 0.0
    monthly_revenue: float = 0.0
    winback_pct_of_revenue: float = 0.0
    winback_sanity_warning: bool = False
    recovery_rate: float = DEFAULT_RECOVERY_RATE
    tier_a_winnable: list[CustomerProfile] = field(default_factory=list)
    tier_b_winnable: list[CustomerProfile] = field(default_factory=list)
    period_days: int = 35


def _observed_monthly_spend(c: CustomerProfile) -> float:
    """Actual spend rate while active = total_spend / active_span_months."""
    if c.first_visit is None or c.last_visit is None:
        return 0.0
    span_days = (c.last_visit - c.first_visit).days
    if span_days < 1:
        return 0.0
    return c.total_spend / span_days * 30


def compute_headlines(
    integrity: IntegrityReport,
    retention: RetentionReport,
    operations: OperationsReport,
    recovery_rate: float = DEFAULT_RECOVERY_RATE,
    winnable_gap_max_days: int = WINNABLE_GAP_MAX_DAYS,
) -> HeadlineNumbers:
    period_days = integrity.venue_baseline.period_days or 35
    monthly_scale = 30 / period_days

    flagged = [s for s in integrity.staff_integrity if s.integrity_score < FLAGGED_SCORE_THRESHOLD]
    flagged_leakage_period = sum(s.total_leakage for s in flagged)
    monthly_leakage = flagged_leakage_period * monthly_scale

    winnable_a: list[CustomerProfile] = []
    winnable_b: list[CustomerProfile] = []
    for c in retention.customers:
        if c.days_since_last > winnable_gap_max_days:
            continue
        if c.median_cadence_days is None or c.median_cadence_days <= 0:
            continue
        if c.is_lapsed_regular:
            winnable_a.append(c)
        elif c.is_lapsing:
            winnable_b.append(c)

    def _recoverable(c: CustomerProfile) -> float:
        return _observed_monthly_spend(c) * recovery_rate

    tier_a_monthly = sum(_recoverable(c) for c in winnable_a)
    tier_b_monthly = sum(_recoverable(c) for c in winnable_b)
    total_winback = tier_a_monthly + tier_b_monthly

    monthly_revenue = retention.total_revenue * monthly_scale
    wb_pct = total_winback / monthly_revenue if monthly_revenue > 0 else 0.0

    return HeadlineNumbers(
        monthly_leakage=monthly_leakage,
        monthly_leakage_flagged_staff=flagged,
        venue_wide_leakage_monthly=integrity.estimated_leakage_monthly,
        monthly_winback_tier_a=tier_a_monthly,
        monthly_winback_total=total_winback,
        monthly_revenue=monthly_revenue,
        winback_pct_of_revenue=wb_pct,
        winback_sanity_warning=wb_pct > SANITY_WINBACK_PCT_WARN,
        recovery_rate=recovery_rate,
        tier_a_winnable=sorted(winnable_a, key=lambda c: _recoverable(c), reverse=True),
        tier_b_winnable=sorted(winnable_b, key=lambda c: _recoverable(c), reverse=True),
        period_days=period_days,
    )


def _pkr(v: float) -> str:
    return f"PKR {v:,.0f}"


def _pct(v: float) -> str:
    return f"{v:.1%}"


def _esc(s: str) -> str:
    return escape(str(s))


def generate_report(
    integrity: IntegrityReport,
    retention: RetentionReport,
    operations: OperationsReport,
) -> str:
    h = compute_headlines(integrity, retention, operations)
    worst = next((s for s in integrity.staff_integrity if s.staff_id == integrity.worst_offender), None)
    top_events = integrity.flagged_events[:3]

    weekday_avg = 0.0
    weekend_avg = 0.0
    wd_count = we_count = 0
    for d in operations.days_of_week:
        if d.day < 5:
            weekday_avg += d.avg_orders
            wd_count += 1
        else:
            weekend_avg += d.avg_orders
            we_count += 1
    weekday_avg = weekday_avg / wd_count if wd_count else 0
    weekend_avg = weekend_avg / we_count if we_count else 0
    weekend_lift = ((weekend_avg - weekday_avg) / weekday_avg * 100) if weekday_avg else 0

    core_dayparts = [dp for dp in operations.dayparts if dp.name in ("Morning", "Midday", "Afternoon", "Evening")]
    busiest_dp = max(core_dayparts, key=lambda d: d.order_count) if core_dayparts else None
    deadest_dp = min(core_dayparts, key=lambda d: d.order_count) if core_dayparts else None

    digital_pct = sum(ps.share_pct for ps in operations.payment_shares if ps.method != "cash")

    heroes = [i for i in operations.items_by_margin if i.margin is not None and i.margin > 0.75][-3:]
    heroes.reverse()
    dogs = [i for i in operations.items_by_margin if i.margin is not None and i.margin < 0.30][:1]

    def _recoverable_val(c):
        return _observed_monthly_spend(c) * h.recovery_rate

    recovery_pct = int(h.recovery_rate * 100)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Venue Audit Report</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #1a1a2e; background: #f8f9fa; line-height: 1.5; padding: 24px; max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: #666; margin-bottom: 24px; }}
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 28px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card.leak {{ border-left: 5px solid #e74c3c; }}
  .card.win  {{ border-left: 5px solid #27ae60; }}
  .card-label {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; color: #888; }}
  .card-value {{ font-size: 32px; font-weight: 800; margin: 6px 0 4px; }}
  .card.leak .card-value {{ color: #c0392b; }}
  .card.win  .card-value {{ color: #1e8449; }}
  .card-sub {{ font-size: 12px; color: #777; }}
  section {{ background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  section h2 {{ font-size: 15px; font-weight: 700; text-transform: uppercase; letter-spacing: .4px;
                color: #555; border-bottom: 2px solid #eee; padding-bottom: 6px; margin-bottom: 14px; }}
  .insight {{ font-size: 13px; color: #555; font-style: italic; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 8px; }}
  th {{ text-align: left; font-weight: 600; color: #888; font-size: 11px; text-transform: uppercase;
       padding: 6px 8px; border-bottom: 2px solid #eee; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #f0f0f0; }}
  tr:last-child td {{ border-bottom: none; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .tag {{ display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; }}
  .tag-red {{ background: #fde8e8; color: #c0392b; }}
  .tag-green {{ background: #e8f8ef; color: #1e8449; }}
  .tag-amber {{ background: #fef3e2; color: #b7791f; }}
  .tag-blue {{ background: #e8f0fe; color: #2c5282; }}
  .kv {{ display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }}
  .kv-label {{ color: #777; }}
  .kv-value {{ font-weight: 600; }}
  .footer {{ text-align: center; font-size: 11px; color: #aaa; margin-top: 20px; }}
  @media print {{ body {{ padding: 0; background: #fff; }} .card {{ break-inside: avoid; }} section {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<h1>Venue Audit Report</h1>
<div class="subtitle">{h.period_days}-day analysis &middot; {_esc(_pkr(retention.total_revenue))} total revenue &middot; {retention.total_orders:,} orders</div>

<div class="cards">
  <div class="card leak">
    <div class="card-label">Monthly Leakage</div>
    <div class="card-value">{_esc(_pkr(h.monthly_leakage))}</div>
    <div class="card-sub">Attributable to {len(h.monthly_leakage_flagged_staff)} flagged staff &middot; Venue-wide: {_esc(_pkr(h.venue_wide_leakage_monthly))}/mo</div>
  </div>
  <div class="card win">
    <div class="card-label">Potential Recoverable / Month</div>
    <div class="card-value">{_esc(_pkr(h.monthly_winback_tier_a))}</div>
    <div class="card-sub">Tier A regulars ({len(h.tier_a_winnable)}) at {recovery_pct}% recovery &middot; A+B: {_esc(_pkr(h.monthly_winback_total))}/mo &middot; {_pct(h.winback_pct_of_revenue)} of revenue</div>
  </div>
</div>
"""

    # ── Integrity section ──
    html += """<section>
<h2>Integrity</h2>
"""
    if worst:
        html += f"""<p class="insight">Staff <strong>{_esc(worst.staff_name)}</strong> ({_esc(worst.staff_id)}) is the primary outlier &mdash;
void rate {_pct(worst.void_rate)}, comp rate {_pct(worst.comp_rate)}, vs venue baseline
{_pct(integrity.venue_baseline.void_rate)} / {_pct(integrity.venue_baseline.comp_rate)}.
Integrity score {worst.integrity_score:.0f}/100.</p>
"""

    if top_events:
        html += """<table>
<tr><th>Order</th><th>Staff</th><th>Flag</th><th>Item</th><th class="num">Value</th></tr>
"""
        for e in top_events:
            flag_cls = "tag-red" if e.flag_type == "theft_void" else "tag-amber"
            label = e.flag_type.replace("_", " ").title()
            html += f"""<tr><td>{_esc(e.order_id)}</td><td>{_esc(e.staff_name)}</td>
<td><span class="tag {flag_cls}">{label}</span></td>
<td>{_esc(e.item_name)}</td><td class="num">{_esc(_pkr(e.value))}</td></tr>
"""
        html += "</table>\n"
    html += "</section>\n"

    # ── Retention section ──
    html += f"""<section>
<h2>Retention</h2>
<p class="insight">Repeat rate measured over identifiable customers only ({_pct(retention.coverage_order_pct)} of orders /
{_pct(retention.coverage_revenue_pct)} of revenue have a customer ref). Anonymous cash transactions are excluded.</p>
<div class="kv"><span class="kv-label">Unique customers</span><span class="kv-value">{retention.unique_customers:,}</span></div>
<div class="kv"><span class="kv-label">Repeat rate</span><span class="kv-value">{_pct(retention.repeat_rate)}</span></div>
<div class="kv"><span class="kv-label">Regulars (cadence &le; {retention.cadence_threshold_days:.0f}d)</span><span class="kv-value">{retention.regular_count}</span></div>
<div class="kv"><span class="kv-label">Lapsed regulars (Tier A)</span><span class="kv-value">{len(h.tier_a_winnable)} &middot; {_esc(_pkr(h.monthly_winback_tier_a))}/mo recoverable</span></div>
<div class="kv"><span class="kv-label">At-risk lapsing (Tier A+B)</span><span class="kv-value">{len(h.tier_a_winnable) + len(h.tier_b_winnable)} &middot; {_esc(_pkr(h.monthly_winback_total))}/mo recoverable</span></div>
<div class="kv"><span class="kv-label">Win-back as % of revenue</span><span class="kv-value">{_pct(h.winback_pct_of_revenue)}</span></div>
"""
    if h.winback_sanity_warning:
        html += f"""<p class="insight" style="color:#b7791f">&#9888; Win-back exceeds {_pct(SANITY_WINBACK_PCT_WARN)} of monthly revenue &mdash; review lapse criteria or recovery assumptions before presenting.</p>
"""
    html += f"""<p class="insight">Values based on each customer&#39;s observed spend rate while active, at {recovery_pct}% assumed recovery.</p>
"""
    if h.tier_a_winnable:
        html += """<table style="margin-top:10px">
<tr><th>Customer</th><th class="num">Visits</th><th class="num">Active Span</th><th class="num">Gap</th><th class="num">Hist. Spend/Mo</th><th class="num">Recoverable/Mo</th></tr>
"""
        for c in h.tier_a_winnable[:8]:
            obs = _observed_monthly_spend(c)
            rv = _recoverable_val(c)
            span = (c.last_visit - c.first_visit).days if c.first_visit and c.last_visit else 0
            html += f"""<tr><td>{_esc(c.customer_ref[:12])}</td><td class="num">{c.visit_count}</td>
<td class="num">{span}d</td><td class="num">{c.days_since_last}d</td>
<td class="num">{_esc(_pkr(obs))}</td><td class="num">{_esc(_pkr(rv))}</td></tr>
"""
        html += "</table>\n"
    html += "</section>\n"

    # ── Operations section ──
    html += """<section>
<h2>Operations</h2>
"""
    html += f"""<p class="insight">Weekends run {weekend_lift:.0f}% busier than weekdays.
{"Busiest core daypart: " + busiest_dp.name + " (" + str(busiest_dp.start_hour) + "–" + str(busiest_dp.end_hour) + "). " if busiest_dp else ""}{"Deadest: " + deadest_dp.name + " (" + str(deadest_dp.start_hour) + "–" + str(deadest_dp.end_hour) + ")." if deadest_dp else ""}</p>
<div class="kv"><span class="kv-label">Avg ticket</span><span class="kv-value">{_esc(_pkr(operations.avg_ticket))}</span></div>
<div class="kv"><span class="kv-label">Digital share</span><span class="kv-value">{_pct(digital_pct)}</span></div>
<div class="kv"><span class="kv-label">Weekend lift</span><span class="kv-value">+{weekend_lift:.0f}%</span></div>
"""
    if heroes or dogs:
        html += """<table style="margin-top:10px">
<tr><th>Item</th><th>Category</th><th class="num">Margin</th><th class="num">Volume</th><th></th></tr>
"""
        for i in heroes:
            html += f"""<tr><td>{_esc(i.name)}</td><td>{_esc(i.category)}</td>
<td class="num">{_pct(i.margin)}</td><td class="num">{i.volume:,}</td>
<td><span class="tag tag-green">Hero</span></td></tr>
"""
        for i in dogs:
            html += f"""<tr><td>{_esc(i.name)}</td><td>{_esc(i.category)}</td>
<td class="num">{_pct(i.margin)}</td><td class="num">{i.volume:,}</td>
<td><span class="tag tag-red">Dog</span></td></tr>
"""
        html += "</table>\n"
    html += "</section>\n"

    html += """<div class="footer">Generated by AsaanPay Audit Engine</div>
</body>
</html>"""
    return html
