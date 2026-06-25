from pathlib import Path

from app.ingest import load_dataset
from app.agents.integrity_agent import (
    run_integrity_agent,
    answer_question,
    build_findings,
)
from app.analysis.integrity import analyze_integrity
from app.analysis.reconciliation import reconcile_payments

DATA = Path(__file__).resolve().parent.parent / "data"


def _data():
    return load_dataset(DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv")


# ── Fake Anthropic client (no network / key needed) ──

class _FakeMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class _FakeClient:
    def __init__(self, text="FAKE LLM OUTPUT"):
        self._text = text
        self.calls = 0

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls += 1
            return _FakeMessage(self._outer._text)

    @property
    def messages(self):
        return _FakeClient._Messages(self)


def test_deterministic_run_without_llm():
    orders, menu, staff = _data()
    report = run_integrity_agent(orders, menu, staff, venue_name="Roastery", use_llm=False)
    assert report.llm_used is False
    assert report.executive_summary  # deterministic fallback present
    assert report.findings
    # Bilal (S03) is the planted offender — should top the findings.
    assert "S03" in report.findings[0].subject
    assert report.reconciliation.net_sales > 0
    assert report.integrity.estimated_leakage_period > 0


def test_findings_are_impact_ranked():
    orders, menu, staff = _data()
    integ = analyze_integrity(orders, menu, staff)
    rec = reconcile_payments(orders, menu, staff)
    findings = build_findings(integ, rec)
    impacts = [f.monetary_impact for f in findings]
    assert impacts == sorted(impacts, reverse=True)
    assert [f.rank for f in findings] == list(range(1, len(findings) + 1))
    assert all(f.severity in ("high", "medium", "low") for f in findings)


def test_llm_layer_with_fake_client():
    orders, menu, staff = _data()
    client = _FakeClient("Executive summary from the model.")
    report = run_integrity_agent(orders, menu, staff, client=client, use_llm=True)
    assert report.llm_used is True
    assert report.executive_summary == "Executive summary from the model."
    # top findings got an LLM recommended action
    assert report.findings[0].recommended_action == "Executive summary from the model."
    assert client.calls > 1


def test_answer_question_with_fake_client():
    orders, menu, staff = _data()
    report = run_integrity_agent(orders, menu, staff, use_llm=False)
    client = _FakeClient("The worst offender is S03 (Bilal).")
    ans = answer_question(report, "Who is the worst offender?", client=client)
    assert ans == "The worst offender is S03 (Bilal)."


def test_answer_question_without_client_is_graceful():
    orders, menu, staff = _data()
    report = run_integrity_agent(orders, menu, staff, use_llm=False)
    ans = answer_question(report, "anything?", client=None)
    assert isinstance(ans, str) and ans  # no crash, returns a message
