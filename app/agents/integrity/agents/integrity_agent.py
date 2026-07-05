"""Integrity agent — LLM reasoning over a deterministic financial audit.

The agent never invents numbers. It runs two exact, deterministic engines —
``analyze_integrity`` (behavioural leakage) and ``reconcile_payments`` (payment /
profit / tax reconciliation) — turns their output into a ranked list of
findings, then uses the LLM to:

  * write an owner-facing executive summary,
  * attach a concrete recommended action to each finding,
  * answer free-form questions about the audited data.

If no LLM client / API key is available, every LLM step degrades to a
deterministic fallback so the agent still produces a complete report offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.integrity.analysis.integrity import IntegrityReport, analyze_integrity
from app.agents.integrity.analysis.reconciliation import ReconciliationReport, reconcile_payments
from app.models.canonical import MenuItem, Order, Staff

DEFAULT_VENUE_NAME = "Restaurant"

SEVERITY_HIGH = 10_000.0
SEVERITY_MEDIUM = 2_000.0

MAX_LLM_ACTIONS = 10


@dataclass
class Finding:
    rank: int
    category: str
    subject: str
    severity: str
    monetary_impact: float
    evidence: str
    recommended_action: str = ""


@dataclass
class IntegrityAgentReport:
    venue_name: str
    period_days: int
    integrity: IntegrityReport
    reconciliation: ReconciliationReport
    findings: list[Finding] = field(default_factory=list)
    executive_summary: str = ""
    llm_used: bool = False


def _severity(impact: float) -> str:
    if impact >= SEVERITY_HIGH:
        return "high"
    if impact >= SEVERITY_MEDIUM:
        return "medium"
    return "low"


def build_findings(
    integrity: IntegrityReport, reconciliation: ReconciliationReport
) -> list[Finding]:
    raw: list[Finding] = []

    for si in integrity.staff_integrity:
        if si.excess_theft_void_value > 0:
            raw.append(Finding(
                rank=0, category="theft", subject=f"{si.staff_name} ({si.staff_id})",
                severity=_severity(si.excess_theft_void_value),
                monetary_impact=si.excess_theft_void_value,
                evidence=(
                    f"{si.theft_void_count} fired-then-voided cash items; "
                    f"void rate {si.void_rate:.1%} vs venue "
                    f"{integrity.venue_baseline.void_rate:.1%} (z={si.void_rate_z:.1f})"
                ),
            ))
        if si.excess_comp_value > 0:
            raw.append(Finding(
                rank=0, category="comp_abuse", subject=f"{si.staff_name} ({si.staff_id})",
                severity=_severity(si.excess_comp_value),
                monetary_impact=si.excess_comp_value,
                evidence=(
                    f"comp rate {si.comp_rate:.1%} vs venue "
                    f"{integrity.venue_baseline.comp_rate:.1%} (z={si.comp_rate_z:.1f})"
                ),
            ))
        if si.excess_discount_value > 0:
            raw.append(Finding(
                rank=0, category="discount_abuse", subject=f"{si.staff_name} ({si.staff_id})",
                severity=_severity(si.excess_discount_value),
                monetary_impact=si.excess_discount_value,
                evidence=(
                    f"discount rate {si.discount_rate:.1%} vs venue "
                    f"{integrity.venue_baseline.discount_rate:.1%} (z={si.discount_rate_z:.1f})"
                ),
            ))

    if reconciliation.payment_mismatch_count > 0:
        raw.append(Finding(
            rank=0, category="payment_discrepancy",
            subject=f"{reconciliation.payment_mismatch_count} orders",
            severity=_severity(reconciliation.payment_mismatch_abs_value),
            monetary_impact=reconciliation.payment_mismatch_abs_value,
            evidence=(
                f"collected != expected on {reconciliation.payment_mismatch_count} orders; "
                f"net unreconciled PKR {reconciliation.net_unreconciled:,.0f}"
            ),
        ))

    if reconciliation.tax_anomaly_count > 0:
        raw.append(Finding(
            rank=0, category="tax_anomaly",
            subject=f"{reconciliation.tax_anomaly_count} orders",
            severity=_severity(reconciliation.tax_anomaly_value),
            monetary_impact=reconciliation.tax_anomaly_value,
            evidence=f"orders taxed at the wrong cash/digital rate; "
                     f"approx PKR {reconciliation.tax_anomaly_value:,.0f} impact",
        ))

    raw.sort(key=lambda f: f.monetary_impact, reverse=True)
    for i, f in enumerate(raw, start=1):
        f.rank = i
    return raw


def _context(report: IntegrityAgentReport) -> str:
    integ = report.integrity
    rec = report.reconciliation
    lines = [
        f"Venue: {report.venue_name}",
        f"Period: {report.period_days} days",
        f"Net sales: PKR {rec.net_sales:,.0f}",
        f"Gross profit: PKR {rec.gross_profit:,.0f} (margin {rec.gross_margin:.1%})",
        f"COGS on sold items: PKR {rec.cogs_sold:,.0f}",
        f"Wasted COGS (comped / fired-then-voided): PKR {rec.wasted_cogs:,.0f}",
        f"Estimated leakage (period): PKR {integ.estimated_leakage_period:,.0f} "
        f"(monthly PKR {integ.estimated_leakage_monthly:,.0f})",
        f"  - suspected theft voids: PKR {integ.suspected_theft_value:,.0f}",
        f"  - excess comps: PKR {integ.excess_comp_value:,.0f}",
        f"  - excess discounts: PKR {integ.excess_discount_value:,.0f}",
        f"Payment mismatches: {rec.payment_mismatch_count} "
        f"(net unreconciled PKR {rec.net_unreconciled:,.0f})",
        f"Tax anomalies: {rec.tax_anomaly_count} (PKR {rec.tax_anomaly_value:,.0f})",
        "",
        "Findings (impact-ranked):",
    ]
    for f in report.findings:
        lines.append(
            f"  {f.rank}. [{f.severity}] {f.category} -- {f.subject}: "
            f"PKR {f.monetary_impact:,.0f}. {f.evidence}"
        )
    return "\n".join(lines)


def _fallback_summary(report: IntegrityAgentReport) -> str:
    rec = report.reconciliation
    integ = report.integrity
    parts = [
        f"{report.venue_name}: over {report.period_days} days, net sales "
        f"PKR {rec.net_sales:,.0f} at {rec.gross_margin:.0%} gross margin "
        f"(profit PKR {rec.gross_profit:,.0f}).",
        f"Estimated leakage PKR {integ.estimated_leakage_period:,.0f} for the period "
        f"(~PKR {integ.estimated_leakage_monthly:,.0f}/month).",
    ]
    if report.findings:
        top = report.findings[0]
        parts.append(
            f"Top issue: {top.category.replace('_', ' ')} on {top.subject} "
            f"(PKR {top.monetary_impact:,.0f})."
        )
    if rec.books_balanced:
        parts.append("Payments reconcile arithmetically; leakage is behavioural.")
    else:
        parts.append(
            f"{rec.payment_mismatch_count} payment mismatch(es) and "
            f"{rec.tax_anomaly_count} tax anomaly(ies) need review."
        )
    return " ".join(parts)


def _generate_summary(client, report: IntegrityAgentReport) -> str | None:
    from app.core.llm import get_model, nothink_kwargs
    prompt = (
        "You are a restaurant loss-prevention analyst. Write a concise, owner-facing "
        "executive summary (3-5 sentences) of this POS integrity audit. Use the exact "
        "figures given; do not invent numbers. Be direct about who and how much.\n\n"
        + _context(report)
    )
    try:
        resp = client.chat.completions.create(
            timeout=30.0,
            model=get_model(),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
            **nothink_kwargs(get_model()),
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return None


def _recommend_action(client, finding: Finding, venue_name: str) -> str | None:
    from app.core.llm import get_model, nothink_kwargs
    prompt = (
        f"Restaurant: {venue_name}. A POS integrity audit produced this finding:\n"
        f"Category: {finding.category}\nSubject: {finding.subject}\n"
        f"Impact: PKR {finding.monetary_impact:,.0f}\nEvidence: {finding.evidence}\n\n"
        "Give ONE concrete next action for the owner/manager (max 25 words). "
        "No preamble, just the action."
    )
    try:
        resp = client.chat.completions.create(
            timeout=10.0,
            model=get_model(),
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
            **nothink_kwargs(get_model()),
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return None


def _make_client(client):
    if client is not None:
        return client
    try:
        from app.core.llm import get_client
        return get_client()
    except Exception:
        return None


def run_integrity_agent(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    venue_name: str = DEFAULT_VENUE_NAME,
    client=None,
    use_llm: bool = True,
) -> IntegrityAgentReport:
    integrity = analyze_integrity(orders, menu, staff)
    reconciliation = reconcile_payments(orders, menu, staff)
    findings = build_findings(integrity, reconciliation)

    report = IntegrityAgentReport(
        venue_name=venue_name,
        period_days=reconciliation.period_days or integrity.venue_baseline.period_days,
        integrity=integrity,
        reconciliation=reconciliation,
        findings=findings,
    )

    llm = _make_client(client) if use_llm else None
    summary = _generate_summary(llm, report) if llm is not None else None
    if summary:
        report.executive_summary = summary
        report.llm_used = True
        for f in findings[:MAX_LLM_ACTIONS]:
            action = _recommend_action(llm, f, venue_name)
            if action:
                f.recommended_action = action

    if not report.executive_summary:
        report.executive_summary = _fallback_summary(report)

    return report


def answer_question(report: IntegrityAgentReport, question: str, client=None) -> str:
    from app.core.llm import get_model, nothink_kwargs
    llm = _make_client(client)
    if llm is None:
        return "LLM unavailable — set ZAI_API_KEY to enable Q&A about the audit."
    prompt = (
        "Answer the question using ONLY the audit data below. Cite exact figures. "
        "If the data does not contain the answer, say so.\n\n"
        f"{_context(report)}\n\nQuestion: {question}"
    )
    try:
        resp = llm.chat.completions.create(
            timeout=25.0,
            model=get_model(),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
            **nothink_kwargs(get_model()),
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[question answering failed: {e}]"
