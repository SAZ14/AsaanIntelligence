"""Phone-friendly PDF audit report — zero dependencies, "Asaan Intelligence" style.

HTML reports don't open nicely on a phone, so this renders the audit as a clean,
branded A4 PDF using a tiny hand-rolled writer over the standard PDF fonts
(Helvetica / Helvetica-Bold / Courier) plus filled rectangles for colour. No
external libraries, so it builds anywhere.

Design: green Asaan Intelligence header, KPI cards, a factual "bottom line",
leakage bars, colour-coded priority actions, and a team-integrity table. The
copy is generated from the exact figures — no filler.

``build_audit_pdf(...)`` returns the bytes; ``write_audit_pdf(...)`` saves them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.agents.integrity.agents.integrity_agent import build_findings
from app.agents.integrity.analysis.integrity import IntegrityReport
from app.agents.integrity.analysis.reconciliation import ReconciliationReport

PAGE_W, PAGE_H = 595, 842  # A4 in points
MARGIN = 50
USABLE_W = PAGE_W - 2 * MARGIN

# ── Asaan Intelligence palette ──
GREEN_DARK = (11, 94, 79)     # brand deep green
GREEN = (22, 163, 74)         # accent green
GREEN_MID = (15, 124, 90)
GREEN_LT = (232, 245, 238)    # tint
INK = (26, 32, 28)
MUTED = (107, 114, 128)
RED = (192, 57, 43)
AMBER = (217, 119, 6)
WHITE = (255, 255, 255)
TRACK = (228, 231, 235)


def _money(v: float) -> str:
    return f"PKR {v:,.0f}"


def _sanitize(s: str) -> str:
    return str(s).encode("latin-1", "ignore").decode("latin-1")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rg(c) -> str:
    return f"{c[0] / 255:.3f} {c[1] / 255:.3f} {c[2] / 255:.3f}"


class _PDF:
    def __init__(self) -> None:
        self._pages: list[str] = []
        self._ops: list[str] = []
        self.y = PAGE_H - MARGIN

    # ── pages ──
    def _footer(self) -> None:
        self._ops.append(f"{_rg(TRACK)} rg {MARGIN} 44 {USABLE_W} 0.8 re f\n")
        self._abs(MARGIN, 30, "Asaan Intelligence  -  Restaurant Integrity", "F1", 8, MUTED)
        self._abs_right(PAGE_W - MARGIN, 30, f"{date.today():%d %b %Y}", "F1", 8, MUTED)

    def _flush(self) -> None:
        if self._ops:
            self._footer()
        self._pages.append("".join(self._ops))
        self._ops = []
        self.y = PAGE_H - MARGIN

    def page_break(self) -> None:
        self._flush()

    def space(self, dy: float) -> None:
        self.y -= dy

    def _ensure(self, height: float) -> None:
        if self.y - height < MARGIN + 20:
            self.page_break()

    # ── primitives ──
    def rect(self, x: float, y: float, w: float, h: float, color) -> None:
        self._ops.append(f"{_rg(color)} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f\n")

    def _abs(self, x: float, y: float, text: str, font: str, size: int, color) -> None:
        t = _escape(_sanitize(text))
        self._ops.append(
            f"{_rg(color)} rg BT /{font} {size} Tf 1 0 0 1 {x:.1f} {y:.1f} Tm ({t}) Tj ET\n"
        )

    def _abs_right(self, right: float, y: float, text: str, font: str, size: int, color) -> None:
        cw = 0.6 if font == "F3" else (0.55 if font == "F2" else 0.5)
        w = len(_sanitize(text)) * cw * size
        self._abs(right - w, y, text, font, size, color)

    def _wrap(self, text: str, size: int, char_w: float, indent: float) -> list[str]:
        max_chars = max(8, int((USABLE_W - indent) / (char_w * size)))
        out, cur = [], ""
        for w in text.split(" "):
            cand = w if not cur else f"{cur} {w}"
            if len(cand) <= max_chars:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                cur = w
        if cur:
            out.append(cur)
        return out or [""]

    def line(self, text: str, size: int = 11, bold: bool = False, indent: float = 0,
             mono: bool = False, gap: float = 1.5, color=INK) -> None:
        font = "F3" if mono else ("F2" if bold else "F1")
        char_w = 0.6 if mono else (0.55 if bold else 0.5)
        for chunk in self._wrap(text, size, char_w, indent):
            self._ensure(size * gap)
            self._abs(MARGIN + indent, self.y, chunk, font, size, color)
            self.y -= size * gap

    def bullet(self, text: str, size: int = 10) -> None:
        self._ensure(size * 1.5)
        self.rect(MARGIN + 1, self.y + 2, 3, 3, GREEN)
        for i, chunk in enumerate(self._wrap(text, size, 0.5, 14)):
            if i:
                self._ensure(size * 1.4)
            self._abs(MARGIN + 14, self.y, chunk, "F1", size, INK)
            self.y -= size * 1.4
        self.y -= size * 0.2

    def heading(self, text: str, size: int = 12) -> None:
        self.space(8)
        self._ensure(size * 2)
        self.rect(MARGIN, self.y - 2, 16, size, GREEN)
        self._abs(MARGIN + 24, self.y, _sanitize(text).upper(), "F2", size, GREEN_DARK)
        self.y -= size * 0.7
        self.rect(MARGIN, self.y, USABLE_W, 0.8, TRACK)
        self.y -= size * 1.1

    def kv(self, label: str, value: str, size: int = 11, value_color=INK) -> None:
        self._ensure(size * 1.6)
        self._abs(MARGIN, self.y, label, "F1", size, MUTED)
        self._abs_right(PAGE_W - MARGIN, self.y, value, "F2", size, value_color)
        self.y -= size * 1.6

    def bar(self, label: str, value: float, maxval: float, color, size: int = 10) -> None:
        self._ensure(size * 2.6)
        self._abs(MARGIN, self.y, label, "F1", size, INK)
        self._abs_right(PAGE_W - MARGIN, self.y, _money(value), "F2", size, color)
        self.y -= size * 1.15
        self.rect(MARGIN, self.y, USABLE_W, 6, TRACK)
        w = USABLE_W * (value / maxval if maxval > 0 else 0)
        if w > 0:
            self.rect(MARGIN, self.y, max(w, 1.5), 6, color)
        self.y -= size * 1.5

    def status_row(self, text: str, status_color, size: int = 10) -> None:
        self._ensure(size * 1.6)
        self.rect(MARGIN, self.y - 0.5, 7, 7, status_color)
        self._abs(MARGIN + 14, self.y, text, "F3", size, INK)
        self.y -= size * 1.6

    def vbars(self, points: list[tuple[str, float]], height: float = 64, color=GREEN) -> None:
        """A small per-period vertical bar chart with labels underneath."""
        n = len(points)
        if n == 0:
            return
        self._ensure(height + 28)
        top = self.y
        bottom = top - height
        maxv = max((v for _, v in points), default=0.0) or 1.0
        slot = USABLE_W / n
        bw = min(slot * 0.62, 40)
        for i, (lbl, v) in enumerate(points):
            cx = MARGIN + i * slot + slot / 2
            bh = height * (v / maxv)
            self.rect(cx - bw / 2, bottom, bw, max(bh, 1.0), color)
            self._abs(cx - len(lbl) * 0.5 * 8 / 2, bottom - 12, lbl, "F1", 8, MUTED)
        self.rect(MARGIN, bottom - 0.5, USABLE_W, 0.6, TRACK)
        self.y = bottom - 26

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


# ── content helpers ──

def _header(pdf: _PDF, venue_name: str, period: int) -> None:
    pdf.rect(0, PAGE_H - 96, PAGE_W, 96, GREEN_DARK)
    pdf.rect(0, PAGE_H - 100, PAGE_W, 4, GREEN)
    pdf._abs(MARGIN, PAGE_H - 44, "ASAAN INTELLIGENCE", "F2", 19, WHITE)
    pdf._abs(MARGIN, PAGE_H - 62, "Restaurant Integrity Report", "F1", 11, GREEN_LT)
    pdf._abs(MARGIN, PAGE_H - 84, f"{_sanitize(venue_name)}  -  {period}-day period", "F2", 11, WHITE)
    pdf._abs_right(PAGE_W - MARGIN, PAGE_H - 84, f"{date.today():%d %b %Y}", "F1", 10, GREEN_LT)
    pdf.y = PAGE_H - 96 - 26


def _cards(pdf: _PDF, integ: IntegrityReport, rec: ReconciliationReport) -> None:
    gap = 12
    w = (USABLE_W - 2 * gap) / 3
    h = 66
    top = pdf.y
    bottom = top - h
    cards = [
        ("MONTHLY LEAKAGE", _money(integ.estimated_leakage_monthly), RED),
        ("GROSS PROFIT", _money(rec.gross_profit), GREEN),
        ("REVENUE", _money(rec.gross_collected), GREEN_DARK),
    ]
    subs = [
        f"{integ.estimated_leakage_period:,.0f} this period".replace(",", ","),
        f"{rec.gross_margin:.0%} margin",
        f"{period_days(rec)} day total",
    ]
    for i, (label, value, accent) in enumerate(cards):
        x = MARGIN + i * (w + gap)
        pdf.rect(x, bottom, w, h, GREEN_LT)
        pdf.rect(x, top - 4, w, 4, accent)
        pdf._abs(x + 10, top - 20, label, "F2", 8, MUTED)
        pdf._abs(x + 10, top - 42, value, "F2", 14, accent)
        pdf._abs(x + 10, top - 56, _sanitize(subs[i]), "F1", 8, MUTED)
    pdf.y = bottom - 20


def period_days(rec: ReconciliationReport) -> int:
    return rec.period_days or 0


def _insights(integ: IntegrityReport, rec: ReconciliationReport) -> list[str]:
    """Short, strictly factual bottom-line bullets derived from the numbers."""
    out: list[str] = []
    bl = integ.venue_baseline
    staff = integ.staff_integrity
    total_leak = sum(s.total_leakage for s in staff) or 0.0
    worst = next((s for s in staff if s.staff_id == integ.worst_offender), None)

    if worst and total_leak > 0:
        share = worst.total_leakage / total_leak
        out.append(
            f"{worst.staff_name} ({worst.staff_id}) accounts for {share:.0%} of all detected "
            f"leakage - integrity score {worst.integrity_score:.0f}/100, the lowest on the team."
        )
        if bl.comp_rate > 0 and worst.comp_rate > bl.comp_rate:
            out.append(
                f"Their comp rate of {worst.comp_rate:.1%} is "
                f"{worst.comp_rate / bl.comp_rate:.1f}x the venue average of {bl.comp_rate:.1%}."
            )
        if bl.void_rate > 0 and worst.theft_void_count > 0:
            out.append(
                f"{worst.theft_void_count} items were voided after being sent to the kitchen on "
                f"cash orders - the classic serve, collect cash, void signature."
            )

    if integ.flagged_events:
        e = integ.flagged_events[0]
        out.append(
            f"Largest single flag: {e.item_name} ({_money(e.value)}) - {e.flag_type.replace('_', ' ')} "
            f"by {e.staff_name}."
        )

    if rec.books_balanced:
        out.append(
            f"Payments reconcile exactly across {rec.total_orders:,} orders and tax is applied "
            f"correctly - the loss is behavioural, not a till error."
        )
    else:
        out.append(
            f"{rec.payment_mismatch_count} payment mismatch(es) and {rec.tax_anomaly_count} tax "
            f"anomaly(ies) detected - books do not fully reconcile."
        )

    digital = sum(m.share_pct for m in rec.by_method if m.method != "cash")
    if digital > 0:
        out.append(
            f"{digital:.0%} of revenue is digital; the remaining cash is where leakage concentrates."
        )
    return out


def build_audit_pdf(
    integrity: IntegrityReport,
    reconciliation: ReconciliationReport,
    venue_name: str = "Restaurant",
    summary: str = "",
) -> bytes:
    integ, rec = integrity, reconciliation
    period = rec.period_days or integ.venue_baseline.period_days
    pdf = _PDF()

    _header(pdf, venue_name, period)
    _cards(pdf, integ, rec)

    # Bottom line
    pdf.heading("The Bottom Line")
    if summary:
        pdf.line(summary, size=10, gap=1.45)
        pdf.space(4)
    for b in _insights(integ, rec):
        pdf.bullet(b)

    # Profit & reconciliation
    pdf.heading("Profit & Reconciliation")
    pdf.kv("Net sales (ex-tax)", _money(rec.net_sales))
    pdf.kv("Cost of goods sold", _money(rec.cogs_sold))
    pdf.kv("Gross profit", f"{_money(rec.gross_profit)}  ({rec.gross_margin:.0%})", value_color=GREEN)
    pdf.kv("Wasted COGS (comp / fired-then-voided)", _money(rec.wasted_cogs), value_color=AMBER)
    pdf.kv("Tax collected", _money(rec.tax_collected))
    pdf.kv("Books balanced", "YES" if rec.books_balanced else "NO - REVIEW",
           value_color=GREEN if rec.books_balanced else RED)
    pdf.space(4)
    pdf.line(f"{'METHOD':<10}{'ORDERS':>9}{'COLLECTED':>17}{'SHARE':>8}", size=9, mono=True, color=MUTED)
    for mb in rec.by_method:
        pdf.line(f"{mb.method:<10}{mb.orders:>9,}{_money(mb.gross_collected):>17}{mb.share_pct:>7.0%}",
                 size=10, mono=True)

    # Leakage breakdown
    pdf.heading("Where The Money Leaks")
    maxv = max(integ.suspected_theft_value, integ.excess_comp_value,
               integ.excess_discount_value, 1.0)
    pdf.bar("Theft voids (cash, voided after firing)", integ.suspected_theft_value, maxv, RED)
    pdf.bar("Excess comps (above venue baseline)", integ.excess_comp_value, maxv, AMBER)
    pdf.bar("Excess discounts (above venue baseline)", integ.excess_discount_value, maxv, GREEN_MID)
    pdf.space(2)
    pdf.kv("Estimated leakage / month", _money(integ.estimated_leakage_monthly), value_color=RED)

    # Priority actions
    findings = build_findings(integ, rec)
    if findings:
        pdf.heading("Priority Actions")
        sev_color = {"high": RED, "medium": AMBER, "low": GREEN_MID}
        for f in findings[:7]:
            issue = f.category.replace("_", " ")
            row = f"{f.rank}. {f.severity.upper():6} {issue:<15} {f.subject[:20]:<20} {_money(f.monetary_impact):>12}"
            pdf.status_row(row, sev_color.get(f.severity, GREEN_MID))
            if f.recommended_action:
                pdf.line(f.recommended_action, size=9, indent=14, color=MUTED, gap=1.4)

    # Team integrity
    roster = sorted(integ.staff_integrity, key=lambda s: s.integrity_score)
    if roster:
        pdf.heading("Team Integrity (worst to best)")
        pdf.line(f"     {'STAFF':<22}{'SCORE':>7}{'LEAKAGE':>15}", size=9, mono=True, color=MUTED)
        for s in roster:
            color = RED if s.integrity_score < 90 else (AMBER if s.integrity_score < 99 else GREEN)
            who = f"{s.staff_name} ({s.staff_id})"
            leak = _money(s.total_leakage) if s.total_leakage > 0 else "-"
            pdf.status_row(f"{who:<22}{s.integrity_score:>7.0f}{leak:>15}", color)

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


# ── Period (daily / weekly) report ──

def _pct_str(curr: float, prev: float) -> str:
    if prev <= 0:
        return "new" if curr > 0 else "-"
    pct = (curr - prev) / prev * 100.0
    return f"{'+' if pct >= 0 else ''}{pct:.0f}% vs prev"


def _period_header(pdf: _PDF, p) -> None:
    title = "Daily Report" if p.kind == "daily" else "Weekly Summary"
    pdf.rect(0, PAGE_H - 96, PAGE_W, 96, GREEN_DARK)
    pdf.rect(0, PAGE_H - 100, PAGE_W, 4, GREEN)
    pdf._abs(MARGIN, PAGE_H - 44, "ASAAN INTELLIGENCE", "F2", 19, WHITE)
    pdf._abs(MARGIN, PAGE_H - 62, f"Restaurant Integrity  -  {title}", "F1", 11, GREEN_LT)
    pdf._abs(MARGIN, PAGE_H - 84, f"{_sanitize(p.venue_name)}  -  {_sanitize(p.label)}", "F2", 11, WHITE)
    pdf._abs_right(PAGE_W - MARGIN, PAGE_H - 84, f"{date.today():%d %b %Y}", "F1", 10, GREEN_LT)
    pdf.y = PAGE_H - 96 - 26


def _period_cards(pdf: _PDF, p) -> None:
    rec = p.report.reconciliation
    integ = p.report.integrity
    prev = p.previous
    gap = 12
    w = (USABLE_W - 2 * gap) / 3
    h = 66
    top = pdf.y
    bottom = top - h
    leak_sub = "this period"
    sales_sub = f"{rec.total_orders:,} orders"
    if prev and prev.has_data:
        sales_sub = _pct_str(p.current.net_sales, prev.net_sales)
        leak_sub = _pct_str(p.current.leakage, prev.leakage)
    cards = [
        ("NET SALES", _money(rec.net_sales), GREEN_DARK, sales_sub),
        ("GROSS PROFIT", _money(rec.gross_profit), GREEN, f"{rec.gross_margin:.0%} margin"),
        ("LEAKAGE", _money(integ.estimated_leakage_period), RED, leak_sub),
    ]
    for i, (label, value, accent, sub) in enumerate(cards):
        x = MARGIN + i * (w + gap)
        pdf.rect(x, bottom, w, h, GREEN_LT)
        pdf.rect(x, top - 4, w, 4, accent)
        pdf._abs(x + 10, top - 20, label, "F2", 8, MUTED)
        pdf._abs(x + 10, top - 42, value, "F2", 14, accent)
        pdf._abs(x + 10, top - 56, _sanitize(sub), "F1", 8, MUTED)
    pdf.y = bottom - 20


def _period_insights(p) -> list[str]:
    rec = p.report.reconciliation
    integ = p.report.integrity
    prev = p.previous
    span = "today" if p.kind == "daily" else "this week"
    out: list[str] = []

    if prev and prev.has_data and prev.net_sales > 0:
        d = (p.current.net_sales - prev.net_sales) / prev.net_sales
        out.append(
            f"Net sales {span} were {_money(rec.net_sales)}, "
            f"{'up' if d >= 0 else 'down'} {abs(d):.0%} on the previous "
            f"{'day' if p.kind == 'daily' else 'week'}."
        )
    else:
        out.append(f"Net sales {span} were {_money(rec.net_sales)} at {rec.gross_margin:.0%} gross margin.")

    if integ.estimated_leakage_period > 0:
        worst = next((s for s in integ.staff_integrity if s.staff_id == integ.worst_offender), None)
        tail = f", concentrated on {worst.staff_name} ({worst.staff_id})" if worst else ""
        line = f"Estimated leakage {span} was {_money(integ.estimated_leakage_period)}{tail}."
        if prev and prev.has_data and prev.leakage > 0:
            d = (p.current.leakage - prev.leakage) / prev.leakage
            line += f" That is {'up' if d >= 0 else 'down'} {abs(d):.0%} on the prior period."
        out.append(line)
    else:
        out.append(f"No behavioural leakage was flagged {span} - clean books.")

    if p.days:
        best = max(p.days, key=lambda d: d.net_sales)
        slow = min((d for d in p.days if d.orders > 0), key=lambda d: d.net_sales, default=None)
        if slow and slow.day != best.day:
            out.append(
                f"Best day was {best.day:%A} ({_money(best.net_sales)}); "
                f"slowest was {slow.day:%A} ({_money(slow.net_sales)})."
            )

    if rec.books_balanced:
        out.append(f"Payments reconcile exactly across {rec.total_orders:,} orders - any loss is behavioural.")
    else:
        out.append(
            f"{rec.payment_mismatch_count} payment mismatch(es) and "
            f"{rec.tax_anomaly_count} tax anomaly(ies) need review."
        )
    return out


def build_period_pdf(p, summary: str = "") -> bytes:
    """Render a daily or weekly :class:`PeriodReport` as a branded PDF."""
    integ = p.report.integrity
    rec = p.report.reconciliation
    pdf = _PDF()

    _period_header(pdf, p)
    _period_cards(pdf, p)

    pdf.heading("The Bottom Line")
    if summary:
        pdf.line(summary, size=10, gap=1.45)
        pdf.space(4)
    for b in _period_insights(p):
        pdf.bullet(b)

    if p.days:
        pdf.heading("Daily Net Sales")
        pdf.vbars([(d.day.strftime("%a"), d.net_sales) for d in p.days])

    pdf.heading("Profit & Reconciliation")
    pdf.kv("Net sales (ex-tax)", _money(rec.net_sales))
    pdf.kv("Cost of goods sold", _money(rec.cogs_sold))
    pdf.kv("Gross profit", f"{_money(rec.gross_profit)}  ({rec.gross_margin:.0%})", value_color=GREEN)
    pdf.kv("Wasted COGS (comp / fired-then-voided)", _money(rec.wasted_cogs), value_color=AMBER)
    pdf.kv("Tax collected", _money(rec.tax_collected))
    pdf.kv("Books balanced", "YES" if rec.books_balanced else "NO - REVIEW",
           value_color=GREEN if rec.books_balanced else RED)

    if integ.estimated_leakage_period > 0:
        pdf.heading("Where The Money Leaks")
        maxv = max(integ.suspected_theft_value, integ.excess_comp_value,
                   integ.excess_discount_value, 1.0)
        pdf.bar("Theft voids (cash, voided after firing)", integ.suspected_theft_value, maxv, RED)
        pdf.bar("Excess comps (above venue baseline)", integ.excess_comp_value, maxv, AMBER)
        pdf.bar("Excess discounts (above venue baseline)", integ.excess_discount_value, maxv, GREEN_MID)

    findings = build_findings(integ, rec)
    if findings:
        pdf.heading("Priority Actions")
        sev_color = {"high": RED, "medium": AMBER, "low": GREEN_MID}
        for f in findings[:6]:
            issue = f.category.replace("_", " ")
            row = f"{f.rank}. {f.severity.upper():6} {issue:<15} {f.subject[:20]:<20} {_money(f.monetary_impact):>12}"
            pdf.status_row(row, sev_color.get(f.severity, GREEN_MID))
            if f.recommended_action:
                pdf.line(f.recommended_action, size=9, indent=14, color=MUTED, gap=1.4)

    return pdf.output()


def write_period_pdf(path: str | Path, p, summary: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_period_pdf(p, summary))
    return path

