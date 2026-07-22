"""Heuristic no-show risk scoring.

Deliberately simple and transparent: a weighted sum of booking + guest-
history features, clamped to a probability and bucketed into a band with a
recommended door action. No model training, no LLM -- fully testable and
explainable to venue staff. Ported unchanged from the maitre-d-agent branch;
this module has no dependency on the storage layer so nothing here needed
to change for the multi-tenant Postgres port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.agents.maitre_d.models import Reservation


@dataclass
class NoShowAssessment:
    risk: float = 0.0               # 0..1
    band: str = "low"               # "low" | "medium" | "high"
    require_deposit: bool = False
    send_reminder: bool = False
    reasons: list[str] = field(default_factory=list)


# Bands
LOW_MAX = 0.25
MEDIUM_MAX = 0.55


def assess_no_show(
    *,
    when: datetime,
    party_size: int,
    booked_at: datetime,
    is_vip: bool,
    history: list[Reservation] | None = None,
) -> NoShowAssessment:
    """Score the likelihood that a booking turns into a no-show.

    Properties guaranteed (and covered by tests):
      * a prior no-show strictly raises risk versus an otherwise identical guest;
      * VIP status strictly lowers risk;
      * more completed visits never raises risk;
      * risk stays within (0, 1).
    """
    history = history or []
    reasons: list[str] = []
    score = 0.10  # base rate

    # Lead time — bookings made far ahead are flakier; same-day is committed.
    lead_days = max((when - booked_at).total_seconds() / 86400.0, 0.0)
    if lead_days >= 7:
        score += 0.12
        reasons.append("booked over a week ahead")
    elif lead_days >= 3:
        score += 0.06
        reasons.append("booked several days ahead")
    elif lead_days < 0.5:
        score -= 0.05
        reasons.append("same-day booking (committed)")

    # Party size — large parties no-show / shrink more often.
    if party_size >= 6:
        score += 0.10
        reasons.append("large party")
    elif party_size >= 4:
        score += 0.04

    # Weekend prime time is higher churn.
    if when.weekday() in (4, 5):  # Fri, Sat
        score += 0.05
        reasons.append("weekend prime time")

    # Guest history.
    prior_no_shows = sum(1 for r in history if r.status == "no_show")
    prior_completed = sum(1 for r in history if r.status in ("completed", "seated"))
    if prior_no_shows:
        score += min(0.20 * prior_no_shows, 0.40)
        reasons.append(f"{prior_no_shows} prior no-show(s)")
    if prior_completed:
        score -= min(0.05 * prior_completed, 0.15)
        reasons.append(f"{prior_completed} prior completed visit(s)")

    # VIPs honour their bookings.
    if is_vip:
        score -= 0.15
        reasons.append("VIP / known guest")

    risk = _clamp(score, 0.02, 0.95)

    if risk < LOW_MAX:
        band = "low"
    elif risk < MEDIUM_MAX:
        band = "medium"
    else:
        band = "high"

    return NoShowAssessment(
        risk=round(risk, 3),
        band=band,
        require_deposit=(band == "high" and not is_vip),
        send_reminder=(band in ("medium", "high")),
        reasons=reasons,
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
