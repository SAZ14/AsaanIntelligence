"""Phone-friendly PDF audit report — zero dependencies.

HTML reports don't open nicely on a phone, so this renders the same audit as a
clean A4 PDF using a tiny hand-rolled writer over the 14 standard PDF fonts
(Helvetica / Helvetica-Bold for prose, Courier for aligned tables). No external
libraries, so it builds anywhere.

``build_audit_pdf(...)`` returns the PDF bytes; ``write_audit_pdf(...)`` saves
them. The layout covers the owner essentials: headline leakage & profit, profit
& reconciliation, leakage breakdown, priority actions, and team integrity.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.agents.integrity_agent import build_findings
from app.analysis.integrity import IntegrityReport
from app.analysis.reconciliation import ReconciliationReport

PAGE_W, PAGE_H = 595, 842  # A4 in points
MARGIN = 50
USABLE_W = PAGE_W - 2 * MARGIN


def _money(v: float) -> str:
    return f"PKR {v:,.0f}"


def _sanitize(s: str) -> str:
    # Standard PDF fonts are Latin-1; drop anything outside it (e.g. emoji).
    return str(s).encode("latin-1", "ignore").decode("latin-1")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _PDF:
    """Minimal text PDF: pages of positioned text lines with auto page-breaks."""

    def __init__(self) -> None:
        self._pages: list[str] = []
        self._ops: list[str] = []
        self.y = PAGE_H - MARGIN

    # ── page handling ──
    def _flush(self) -> None:
        self._pages.append("".join(self._ops))
        self._ops = []
        self.y = PAGE_H - MARGIN

    def page_break(self) -> None:
        self._flush()

    def space(self, dy: float) -> None:
        self.y -= dy

    def _ensure(self, height: float) -> None:
        if self.y - height < MARGIN:
            self.page_break()

    # ── drawing ──
    def _draw(self, text: str, x: float, font: str, size: int) -> None:
        t = _escape(_sanitize(text))
        self._ops.append(f"BT /{font} {size} Tf 1 0 0 1 {x:.1f} {self.y:.1f} Tm ({t}) Tj ET\n")

    def _wrap(self, text: str, size: int, char_w: float, indent: float) -> list[str]:
        max_chars = max(8, int((USABLE_W - indent) / (char_w * size)))
        words = text.split(" ")
        lines: list[str] = []
        cur = ""
        for w in words:
            cand = w if not cur else f"{cur} {w}"
            if len(cand) <= max_chars:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def line(self, text: str, size: int = 11, bold: bool = False,
             indent: float = 0, mono: bool = False, gap: float = 1.5) -> None:
        font = "F3" if mono else ("F2" if bold else "F1")
        char_w = 0.6 if mono else (0.55 if bold else 0.5)
        for chunk in self._wrap(text, size, char_w, indent):
            self._ensure(size * gap)
            self._draw(chunk, MARGIN + indent, font, size)
            self.y -= size * gap

    def heading(self, text: str, size: int = 13) -> None:
        self.space(6)
        self._ensure(size * 1.6)
        self._draw(_sanitize(text).upper(), MARGIN, "F2", size)
        self.y -= size * 0.5
        self._ensure(2)
        self._ops.append(f"{MARGIN} {self.y:.1f} m {PAGE_W - MARGIN} {self.y:.1f} l S\n")
        self.y -= size * 1.0

    def kv(self, label: str, value: str, size: int = 11) -> None:
        self._ensure(size * 1.5)
        self._draw(label, MARGIN, "F1", size)
        # right-align value via a rough width estimate
        vw = len(_sanitize(value)) * 0.5 * size
        self._draw(value, PAGE_W - MARGIN - vw, "F2", size)
        self.y -= size * 1.5

    # ── output ──
    def output(self) -> bytes:
        self._flush()
        objects: dict[int, bytes] = {
            3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        }
        page_nums: list[int] = []
        nxt = 6
        for content in self._pages:
            cnum, pnum = nxt, nxt + 1
            nxt += 2
            stream = content.encode("latin-1", "replace")
            objects[cnum] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
            objects[pnum] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> "
                b"/Contents %d 0 R >>" % cnum
            )
            page_nums.append(pnum)
        kids = b" ".join(b"%d 0 R" % n for n in page_nums)
        objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_nums))
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

        out = b"%PDF-1.4\n"
        offsets: dict[int, int] = {}
        for num in sorted(objects):
            offsets[num] = len(out)
            out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
        xref_pos = len(out)
        size = max(objects) + 1
        out += b"xref\n0 %d\n0000000000 65535 f \n" % size
        for num in range(1, size):
            out += b"%010d 00000 n \n" % offsets[num]
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (size, xref_pos)
        return out


def build_audit_pdf(
    integrity: IntegrityReport,
    reconciliation: ReconciliationReport,
    venue_name: str = "Restaurant",
    summary: str = "",
) -> bytes:
    rec = reconciliation
    integ = integrity
    period = rec.period_days or integ.venue_baseline.period_days
    pdf = _PDF()

    # Title
    pdf.line(venue_name, size=20, bold=True, gap=1.3)
    pdf.line(f"Integrity Audit  -  {period}-day period  -  generated {date.today():%d %b %Y}",
             size=10, gap=2.0)

    # Headline
    pdf.heading("Headline")
    pdf.kv("Estimated leakage (monthly)", _money(integ.estimated_leakage_monthly))
    pdf.kv("Gross profit", f"{_money(rec.gross_profit)}  ({rec.gross_margin:.0%} margin)")
    pdf.kv("Revenue collected", _money(rec.gross_collected))

    if summary:
        pdf.space(4)
        pdf.line(summary, size=10, gap=1.5)

    # Profit & reconciliation
    pdf.heading("Profit & Reconciliation")
    pdf.kv("Net sales (ex-tax)", _money(rec.net_sales))
    pdf.kv("COGS (sold)", _money(rec.cogs_sold))
    pdf.kv("Gross profit", f"{_money(rec.gross_profit)}  ({rec.gross_margin:.0%})")
    pdf.kv("Wasted COGS (comp / fired-then-voided)", _money(rec.wasted_cogs))
    pdf.kv("Tax collected", _money(rec.tax_collected))
    pdf.kv("Books balanced", "yes" if rec.books_balanced else "NO - needs review")
    pdf.kv("Payment mismatches", f"{rec.payment_mismatch_count} ({_money(rec.payment_mismatch_abs_value)})")
    pdf.kv("Tax anomalies", f"{rec.tax_anomaly_count} ({_money(rec.tax_anomaly_value)})")

    if rec.by_method:
        pdf.space(4)
        pdf.line(f"{'Method':<10}{'Orders':>8}{'Collected':>16}{'Share':>8}", size=10, mono=True)
        for mb in rec.by_method:
            pdf.line(f"{mb.method:<10}{mb.orders:>8,}{_money(mb.gross_collected):>16}{mb.share_pct:>7.0%}",
                     size=10, mono=True)

    # Leakage
    pdf.heading("Leakage")
    pdf.kv("Estimated (this period)", _money(integ.estimated_leakage_period))
    pdf.kv("Estimated (monthly)", _money(integ.estimated_leakage_monthly))
    pdf.kv("Theft voids", _money(integ.suspected_theft_value))
    pdf.kv("Excess comps", _money(integ.excess_comp_value))
    pdf.kv("Excess discounts", _money(integ.excess_discount_value))

    # Priority actions
    findings = build_findings(integ, rec)
    if findings:
        pdf.heading("Priority Actions")
        pdf.line(f"{'#':<3}{'Severity':<9}{'Issue':<16}{'Who':<18}{'Impact':>12}", size=10, mono=True)
        for f in findings[:8]:
            issue = f.category.replace("_", " ")
            pdf.line(f"{f.rank:<3}{f.severity:<9}{issue:<16}{f.subject[:17]:<18}{_money(f.monetary_impact):>12}",
                     size=10, mono=True)

    # Team integrity
    roster = sorted(integ.staff_integrity, key=lambda s: s.integrity_score)
    if roster:
        pdf.heading("Team Integrity (worst to best)")
        pdf.line(f"{'Staff':<22}{'Score':>7}{'Leakage':>14}", size=10, mono=True)
        for s in roster:
            who = f"{s.staff_name} ({s.staff_id})"
            leak = _money(s.total_leakage) if s.total_leakage > 0 else "-"
            pdf.line(f"{who:<22}{s.integrity_score:>6.0f}{leak:>14}", size=10, mono=True)

    pdf.space(10)
    pdf.line("Generated by AsaanPay Integrity Agent", size=9, gap=1.0)
    return pdf.output()


def write_audit_pdf(
    path: str | Path,
    integrity: IntegrityReport,
    reconciliation: ReconciliationReport,
    venue_name: str = "Restaurant",
    summary: str = "",
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_audit_pdf(integrity, reconciliation, venue_name, summary))
    return path
