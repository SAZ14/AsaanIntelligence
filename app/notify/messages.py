"""Render owner-facing WhatsApp message bodies from analysis results."""

from __future__ import annotations

from app.report.render import HeadlineNumbers


def _pkr(v: float) -> str:
    return f"PKR {v:,.0f}"


def owner_summary(h: HeadlineNumbers, *, venue_name: str | None = None) -> str:
    """A concise WhatsApp summary of the two owner-facing headline numbers.

    Kept short and plain-text so it renders well in a chat bubble.
    """
    title = f"*{venue_name} — Audit Summary*" if venue_name else "*Venue Audit Summary*"
    recovery_pct = int(round(h.recovery_rate * 100))

    lines = [
        title,
        f"Period: {h.period_days} days",
        "",
        f"🔴 Monthly leakage: {_pkr(h.monthly_leakage)}",
        f"   ({len(h.monthly_leakage_flagged_staff)} flagged staff; "
        f"venue-wide {_pkr(h.venue_wide_leakage_monthly)}/mo)",
        "",
        f"🟢 Recoverable/mo: {_pkr(h.monthly_winback_tier_a)}",
        f"   (Tier A regulars at {recovery_pct}% recovery; "
        f"A+B {_pkr(h.monthly_winback_total)}/mo, "
        f"{h.winback_pct_of_revenue:.1%} of revenue)",
    ]
    if h.winback_sanity_warning:
        lines.append("")
        lines.append("⚠ Win-back exceeds 10% of monthly revenue — review assumptions.")
    return "\n".join(lines)
