"""Daily and weekly period reports.

The audit engines (``reconcile_payments`` / ``analyze_integrity``) compute every
figure for *whatever* set of orders they are handed. So a "daily" or "weekly"
report is just the same audit run over a date window — plus a comparison against
the previous window so the owner sees direction, not only a number.

  * **Daily report** — one business day (defaults to the latest day with data),
    compared to the day before.
  * **Weekly report** — a 7-day window ending on the latest day, compared to the
    previous 7 days, with a per-day sales/leakage breakdown for the trend.

Everything here is deterministic; the LLM narrative is off by default so digests
are instant and free. Formatting for WhatsApp lives in
``app/whatsapp/service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.agents.integrity.agents.integrity_agent import IntegrityAgentReport, run_integrity_agent
from app.models.canonical import MenuItem, Order, Staff


@dataclass
class PeriodMetrics:
    """Headline figures used for period-over-period comparison."""

    net_sales: float = 0.0
    gross_profit: float = 0.0
    gross_margin: float = 0.0
    leakage: float = 0.0
    orders: int = 0
    has_data: bool = False


@dataclass
class DayPoint:
    day: date
    net_sales: float
    leakage: float
    orders: int


@dataclass
class PeriodReport:
    venue_name: str
    kind: str  # "daily" | "weekly"
    start: date
    end: date
    label: str
    report: IntegrityAgentReport  # the windowed audit
    current: PeriodMetrics
    previous: PeriodMetrics | None = None  # prior window, for deltas
    days: list[DayPoint] = field(default_factory=list)  # per-day (weekly)


# ── Windowing ──

def latest_date(orders: list[Order]) -> date | None:
    if not orders:
        return None
    return max(o.datetime for o in orders).date()


def slice_orders(orders: list[Order], start: date, end: date) -> list[Order]:
    """Orders whose date falls within [start, end] inclusive."""
    return [o for o in orders if start <= o.datetime.date() <= end]


# ── Metrics ──

def _metrics(report: IntegrityAgentReport, had_orders: bool) -> PeriodMetrics:
    rec = report.reconciliation
    integ = report.integrity
    return PeriodMetrics(
        net_sales=rec.net_sales,
        gross_profit=rec.gross_profit,
        gross_margin=rec.gross_margin,
        leakage=integ.estimated_leakage_period,
        orders=rec.total_orders,
        has_data=had_orders,
    )


def _window_report(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    venue_name: str,
    start: date,
    end: date,
    use_llm: bool = False,
) -> tuple[IntegrityAgentReport, list[Order]]:
    windowed = slice_orders(orders, start, end)
    report = run_integrity_agent(
        windowed, menu, staff, venue_name=venue_name, use_llm=use_llm
    )
    return report, windowed


def _fmt_day(d: date) -> str:
    """Format day number without zero-padding (cross-platform)."""
    return str(d.day)


def _label(start: date, end: date) -> str:
    if start == end:
        return f"{start.strftime('%a')} {_fmt_day(start)} {start.strftime('%b %Y')}"
    if start.month == end.month and start.year == end.year:
        return f"{_fmt_day(start)}–{_fmt_day(end)} {end.strftime('%b %Y')}"
    return f"{_fmt_day(start)} {start.strftime('%b')} – {_fmt_day(end)} {end.strftime('%b %Y')}"


# ── Builders ──

def build_daily_report(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    venue_name: str = "Restaurant",
    day: date | None = None,
    use_llm: bool = False,
) -> PeriodReport | None:
    """Audit for a single day, compared to the day before."""
    end = day or latest_date(orders)
    if end is None:
        return None

    report, windowed = _window_report(
        orders, menu, staff, venue_name, end, end, use_llm=use_llm
    )

    prev_day = end - timedelta(days=1)
    prev_report, prev_orders = _window_report(
        orders, menu, staff, venue_name, prev_day, prev_day
    )

    return PeriodReport(
        venue_name=venue_name,
        kind="daily",
        start=end,
        end=end,
        label=_label(end, end),
        report=report,
        current=_metrics(report, bool(windowed)),
        previous=_metrics(prev_report, bool(prev_orders)),
    )


def build_weekly_report(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    venue_name: str = "Restaurant",
    end_day: date | None = None,
    days: int = 7,
    use_llm: bool = False,
) -> PeriodReport | None:
    """Audit for a ``days``-day window, compared to the preceding window."""
    end = end_day or latest_date(orders)
    if end is None:
        return None
    start = end - timedelta(days=days - 1)

    report, windowed = _window_report(
        orders, menu, staff, venue_name, start, end, use_llm=use_llm
    )

    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    prev_report, prev_orders = _window_report(
        orders, menu, staff, venue_name, prev_start, prev_end
    )

    # Per-day breakdown for the trend line.
    points: list[DayPoint] = []
    for i in range(days):
        d = start + timedelta(days=i)
        d_report, d_orders = _window_report(orders, menu, staff, venue_name, d, d)
        points.append(DayPoint(
            day=d,
            net_sales=d_report.reconciliation.net_sales,
            leakage=d_report.integrity.estimated_leakage_period,
            orders=d_report.reconciliation.total_orders,
        ))

    return PeriodReport(
        venue_name=venue_name,
        kind="weekly",
        start=start,
        end=end,
        label=_label(start, end),
        report=report,
        current=_metrics(report, bool(windowed)),
        previous=_metrics(prev_report, bool(prev_orders)),
        days=points,
    )

