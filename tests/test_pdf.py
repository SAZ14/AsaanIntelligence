from pathlib import Path

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity
from app.analysis.reconciliation import reconcile_payments
from app.report.pdf import build_audit_pdf, write_audit_pdf

DATA = Path(__file__).resolve().parent.parent / "data"


def _reports():
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    return analyze_integrity(orders, menu, staff), reconcile_payments(orders, menu, staff)


def test_pdf_bytes_are_valid():
    integ, rec = _reports()
    pdf = build_audit_pdf(integ, rec, venue_name="Roastery")
    assert pdf.startswith(b"%PDF-1.")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in pdf
    assert b"/Type /Page" in pdf
    assert len(pdf) > 1500


def test_pdf_handles_non_latin1_summary():
    integ, rec = _reports()
    # emoji / non-Latin-1 chars must not break the writer
    pdf = build_audit_pdf(integ, rec, venue_name="Café 🚀", summary="Profit 📈 up — leakage 🩸 down")
    assert pdf.startswith(b"%PDF-1.")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_write_audit_pdf(tmp_path):
    integ, rec = _reports()
    out = write_audit_pdf(tmp_path / "sub" / "audit.pdf", integ, rec, venue_name="Roastery")
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-1.")
