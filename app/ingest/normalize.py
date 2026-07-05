"""Universal CSV normalization — accept any POS export, emit canonical CSV.

Every CSV uploaded via WhatsApp passes through here before storage. The
pipeline translates arbitrary column names, date formats, and number formats
into the canonical schema (`app/ingest/mappings/cafe_generic.py` column
names, ISO dates, plain numbers) so every downstream consumer — the
integrity connector, the revenue datasource — keeps parsing one fixed,
deterministic format.

Resolution order for an incoming file's headers:
  1. Stored mapping (pos_column_mappings) matching this store + file type +
     header fingerprint → deterministic, no LLM.
  2. Identity: headers already canonical (after light normalization).
  3. LLM inference (fast model, one call) → validated against the actual
     rows before being trusted or stored. A mapping that fails validation
     is never saved.

Values are parsed tolerantly: a dozen date formats (dd/mm/yyyy, AM/PM,
"05-Jul-2026", ISO, epoch), currency-decorated amounts ("Rs. 1,050.50",
"(500)" negatives), many boolean spellings, and split Date + Time columns.
Missing optional fields get honest defaults and are reported back to staff
as warnings, not silent guesses.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Canonical schemas ─────────────────────────────────────────────────────────
# Field -> short description used in the LLM prompt. Order matters: it is the
# column order of the emitted canonical CSV.

SALES_FIELDS: dict[str, str] = {
    "order_id": "receipt/order/invoice/bill number that groups line items into one order",
    "datetime": "when the order happened (date+time; may be split across a date column and a time column)",
    "staff_id": "cashier/waiter id",
    "staff_name": "cashier/waiter name",
    "table": "table number/name",
    "channel": "dine-in/takeaway/delivery/online",
    "item_sku": "product code/sku/barcode",
    "item_name": "product/item/dish name",
    "category": "item category",
    "qty": "quantity of the item in this line",
    "unit_price": "price per unit",
    "line_amount": "total for the line (qty x unit price, possibly after discount)",
    "discount_amount": "discount on this line or order",
    "is_void": "whether the line/order was voided/cancelled",
    "void_after_fire": "voided after being sent to kitchen",
    "is_comp": "complimentary/free item",
    "order_status": "order status (closed/completed/cancelled...)",
    "payment_method": "cash/card/online payment type",
    "payment_amount": "total amount paid for the whole order",
    "tax_rate": "tax rate or percent",
    "customer_ref": "customer name/phone/reference",
}
MENU_FIELDS: dict[str, str] = {
    "sku": "product code/sku",
    "name": "item/dish name",
    "category": "item category",
    "cost": "cost/COGS per unit (what the restaurant pays)",
    "price": "selling price",
}
STAFF_FIELDS: dict[str, str] = {
    "staff_id": "employee id/code",
    "name": "employee name",
    "role": "job title/role",
}

SCHEMAS: dict[str, dict[str, str]] = {
    "pos_sales": SALES_FIELDS,
    "pos_menu": MENU_FIELDS,
    "pos_staff": STAFF_FIELDS,
}

# Fields that MUST resolve to real data or the file is rejected.
CRITICAL: dict[str, set[str]] = {
    "pos_sales": {"order_id", "datetime", "item_name"},
    "pos_menu": {"name", "price"},
    "pos_staff": {"name"},
}

# Minimum fraction of data rows that must survive parsing.
MIN_ROW_SUCCESS = 0.90


@dataclass
class NormalizeResult:
    ok: bool
    canonical_csv: str = ""
    rows_in: int = 0
    rows_out: int = 0
    mapping_source: str = ""       # "stored" | "identity" | "llm"
    mapped_columns: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


# ── Reading: delimiter sniffing, BOM, blank lines ────────────────────────────

def read_csv_text(content: str) -> tuple[list[str], list[dict]]:
    """Parse CSV text into (headers, rows). Handles BOM, sniffs the delimiter
    (comma/semicolon/tab/pipe), skips fully blank lines."""
    text = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        raise ValueError("file is empty")
    sample = "\n".join(lines[:10])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
    if not headers:
        raise ValueError("no header row found")
    rows = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if any(row.values()):
            rows.append(row)
    return headers, rows


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def header_fingerprint(headers: list[str]) -> str:
    joined = "|".join(sorted(_norm_header(h) for h in headers))
    return hashlib.sha1(joined.encode()).hexdigest()


# ── Tolerant value parsers ────────────────────────────────────────────────────

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    "%d-%b-%Y %H:%M", "%d-%b-%Y %I:%M %p", "%d-%b-%Y", "%d %b %Y %H:%M", "%d %b %Y",
    "%d-%B-%Y", "%d %B %Y",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
]


def parse_datetime_value(value: str) -> datetime | None:
    """Parse a datetime from the formats POS systems actually emit.

    Day-first formats are tried before month-first because this platform's
    market (Pakistan) writes dd/mm/yyyy; an unambiguous month-first value
    (13/05 → month 13 invalid) still falls through to the right format."""
    v = value.strip()
    if not v:
        return None
    # epoch seconds/millis
    if re.fullmatch(r"\d{10}(\d{3})?", v):
        try:
            ts = int(v[:10])
            if 946684800 <= ts <= 4102444800:  # 2000..2100
                return datetime.utcfromtimestamp(ts)
        except (ValueError, OSError):
            pass
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:  # last resort: dateutil handles the long tail
        from dateutil import parser as _du
        return _du.parse(v, dayfirst=True)
    except Exception:
        return None


_NUM_JUNK_RE = re.compile(r"(rs\.?|pkr|rupees?|₨|\$|€|£|%|,|\s)", re.I)


def parse_number_value(value: str) -> float | None:
    v = value.strip()
    if not v or v.lower() in ("na", "n/a", "-", "--", "null", "none"):
        return None
    negative = v.startswith("(") and v.endswith(")")
    v = _NUM_JUNK_RE.sub("", v.strip("()"))
    if not v or not re.fullmatch(r"-?\d*\.?\d+", v):
        return None
    n = float(v)
    return -n if negative else n


_TRUE_WORDS = {"1", "true", "yes", "y", "void", "voided", "cancelled", "canceled",
               "comp", "free", "refund", "refunded", "haan"}
_FALSE_WORDS = {"", "0", "false", "no", "n", "none", "na", "n/a", "-", "completed",
                "closed", "paid", "done", "nahi"}


def parse_bool_value(value: str) -> bool:
    return value.strip().lower() in _TRUE_WORDS


# ── Identity mapping (headers already canonical) ─────────────────────────────

def identity_mapping(headers: list[str], file_type: str) -> dict | None:
    """If (normalized) headers already use canonical names, map them directly."""
    schema = SCHEMAS[file_type]
    norm_to_source = {_norm_header(h): h for h in headers}
    colmap = {}
    for fieldname in schema:
        src = norm_to_source.get(_norm_header(fieldname))
        if src is not None:
            colmap[fieldname] = src
    if CRITICAL[file_type] <= set(colmap):
        return colmap
    return None


# ── LLM mapping inference ─────────────────────────────────────────────────────

def infer_mapping_llm(headers: list[str], sample_rows: list[dict], file_type: str) -> dict | None:
    """One fast-model call proposing canonical field -> source column.

    Returns None on any failure — the caller reports a clear error to staff
    rather than guessing. The proposal is never trusted without
    validate_and_normalize() passing on the real rows."""
    from app.core.llm import get_client, get_fast_model

    schema = SCHEMAS[file_type]
    schema_desc = "\n".join(f'- "{k}": {v}' for k, v in schema.items())
    samples = "\n".join(
        json.dumps({h: r.get(h, "") for h in headers}, ensure_ascii=False)
        for r in sample_rows[:4]
    )
    prompt = (
        "You map columns of a restaurant POS export to a canonical schema.\n\n"
        f"CANONICAL FIELDS:\n{schema_desc}\n\n"
        f"SOURCE COLUMNS: {json.dumps(headers, ensure_ascii=False)}\n\n"
        f"SAMPLE ROWS:\n{samples}\n\n"
        "Reply with ONLY a JSON object: canonical field -> source column name "
        "(copied EXACTLY from SOURCE COLUMNS), or null when no source column exists. "
        'Special case: if date and time are separate columns, map "datetime" to a '
        "two-element array [date_column, time_column].\n"
        "Never map two canonical fields to the same source column unless the data "
        "genuinely serves both. Never invent column names."
    )
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=get_fast_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=500,
            timeout=20.0,
        )
        body = (resp.choices[0].message.content or "").strip()
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body).strip()
        proposal = json.loads(body)
    except Exception as exc:
        logger.warning("normalize.infer: LLM mapping failed (%s)", exc)
        return None

    if not isinstance(proposal, dict):
        return None
    valid_headers = set(headers)
    colmap: dict = {}
    for fieldname in SCHEMAS[file_type]:
        src = proposal.get(fieldname)
        if src is None:
            continue
        if isinstance(src, list):
            if fieldname == "datetime" and len(src) == 2 and all(s in valid_headers for s in src):
                colmap[fieldname] = src
            continue
        if isinstance(src, str) and src in valid_headers:
            colmap[fieldname] = src
    return colmap or None


# ── Validation + canonical emission ──────────────────────────────────────────

def _src_value(row: dict, colmap: dict, fieldname: str) -> str:
    src = colmap.get(fieldname)
    if src is None:
        return ""
    if isinstance(src, list):  # [date_col, time_col]
        return " ".join(row.get(c, "").strip() for c in src).strip()
    return row.get(src, "").strip()


def validate_and_normalize(rows: list[dict], colmap: dict, file_type: str) -> NormalizeResult:
    """Apply a column mapping to real rows; emit canonical CSV or a clear error.

    This is the trust gate for LLM-proposed mappings: critical fields must
    parse on at least MIN_ROW_SUCCESS of rows or the mapping is rejected."""
    res = NormalizeResult(ok=False, rows_in=len(rows), mapped_columns=dict(colmap))
    if not rows:
        res.error = "The file has a header but no data rows."
        return res

    missing_critical = CRITICAL[file_type] - set(colmap)
    if missing_critical:
        res.error = (
            "Could not find required column(s): "
            + ", ".join(sorted(missing_critical))
            + ". Please check this is the right export."
        )
        return res

    if file_type == "pos_sales":
        return _normalize_sales(rows, colmap, res)
    if file_type == "pos_menu":
        return _normalize_menu(rows, colmap, res)
    return _normalize_staff(rows, colmap, res)


def _finish(res: NormalizeResult, header: list[str], out_rows: list[list], dropped: int) -> NormalizeResult:
    if not out_rows:
        res.error = "No rows could be parsed with the detected columns."
        return res
    if len(out_rows) / max(1, res.rows_in) < MIN_ROW_SUCCESS:
        res.error = (
            f"Only {len(out_rows)} of {res.rows_in} rows parsed cleanly — "
            "the column mapping looks wrong for this file."
        )
        return res
    if dropped:
        res.warnings.append(f"{dropped} unreadable row(s) skipped.")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(out_rows)
    res.canonical_csv = buf.getvalue()
    res.rows_out = len(out_rows)
    res.ok = True
    return res


def _fmt_num(n: float | None, default: str = "") -> str:
    if n is None:
        return default
    return f"{n:.10g}"


def _normalize_sales(rows: list[dict], colmap: dict, res: NormalizeResult) -> NormalizeResult:
    header = list(SALES_FIELDS)
    have = set(colmap)
    out: list[list] = []
    dropped = 0

    # Warnings for meaningful absences — staff should know what analyses degrade.
    if "is_void" not in have and "order_status" not in have:
        res.warnings.append("No void/cancellation column — void analysis unavailable.")
    if "unit_price" not in have and "line_amount" not in have:
        res.error = "Found no price or amount column — cannot analyze sales without one."
        return res
    if "qty" not in have:
        res.warnings.append("No quantity column — assuming 1 per line.")
    if "payment_amount" not in have:
        res.warnings.append("No paid-total column — payment totals derived from line amounts.")

    order_line_sums: dict[str, float] = {}
    parsed: list[dict] = []
    for row in rows:
        order_id = _src_value(row, colmap, "order_id")
        dt_val = parse_datetime_value(_src_value(row, colmap, "datetime"))
        item_name = _src_value(row, colmap, "item_name")
        if not order_id or dt_val is None or not item_name:
            dropped += 1
            continue

        qty_n = parse_number_value(_src_value(row, colmap, "qty")) if "qty" in have else None
        qty = int(qty_n) if qty_n and qty_n > 0 else 1
        unit_price = parse_number_value(_src_value(row, colmap, "unit_price")) if "unit_price" in have else None
        line_amount = parse_number_value(_src_value(row, colmap, "line_amount")) if "line_amount" in have else None
        # Derive whichever of the two amounts is missing.
        if line_amount is None and unit_price is not None:
            line_amount = unit_price * qty
        if unit_price is None and line_amount is not None:
            unit_price = line_amount / qty
        if line_amount is None:
            dropped += 1
            continue

        discount = parse_number_value(_src_value(row, colmap, "discount_amount")) or 0.0
        status = _src_value(row, colmap, "order_status")
        is_void = parse_bool_value(_src_value(row, colmap, "is_void")) if "is_void" in have \
            else status.lower() in ("void", "voided", "cancelled", "canceled", "refunded")
        payment_amount = parse_number_value(_src_value(row, colmap, "payment_amount")) if "payment_amount" in have else None
        tax_rate = parse_number_value(_src_value(row, colmap, "tax_rate")) or 0.0
        if tax_rate > 1:  # "16" or "16%" means 16 percent
            tax_rate = tax_rate / 100.0

        if not is_void:
            order_line_sums[order_id] = order_line_sums.get(order_id, 0.0) + line_amount - discount

        parsed.append({
            "order_id": order_id,
            "datetime": dt_val.isoformat(sep=" ", timespec="seconds"),
            "staff_id": _src_value(row, colmap, "staff_id") or _src_value(row, colmap, "staff_name") or "unknown",
            "staff_name": _src_value(row, colmap, "staff_name") or _src_value(row, colmap, "staff_id") or "unknown",
            "table": _src_value(row, colmap, "table"),
            "channel": _src_value(row, colmap, "channel"),
            "item_sku": _src_value(row, colmap, "item_sku") or item_name,
            "item_name": item_name,
            "category": _src_value(row, colmap, "category"),
            "qty": str(qty),
            "unit_price": _fmt_num(unit_price),
            "line_amount": _fmt_num(line_amount),
            "discount_amount": _fmt_num(discount, "0"),
            "is_void": "true" if is_void else "false",
            "void_after_fire": "true" if parse_bool_value(_src_value(row, colmap, "void_after_fire")) else "false",
            "is_comp": "true" if parse_bool_value(_src_value(row, colmap, "is_comp")) else "false",
            "order_status": status or ("void" if is_void else "closed"),
            "payment_method": _src_value(row, colmap, "payment_method") or "unknown",
            "_payment_amount": payment_amount,  # resolved below
            "tax_rate": _fmt_num(tax_rate, "0"),
            "customer_ref": _src_value(row, colmap, "customer_ref"),
        })

    for p in parsed:
        amount = p.pop("_payment_amount")
        if amount is None:
            amount = order_line_sums.get(p["order_id"], 0.0)
        p["payment_amount"] = _fmt_num(amount, "0")
        out.append([p[f] for f in header])

    return _finish(res, header, out, dropped)


def _normalize_menu(rows: list[dict], colmap: dict, res: NormalizeResult) -> NormalizeResult:
    header = list(MENU_FIELDS)
    have = set(colmap)
    if "cost" not in have:
        res.warnings.append("No cost/COGS column — profit-margin analysis will be limited.")
    out: list[list] = []
    dropped = 0
    for row in rows:
        name = _src_value(row, colmap, "name")
        price = parse_number_value(_src_value(row, colmap, "price"))
        if not name or price is None:
            dropped += 1
            continue
        cost = parse_number_value(_src_value(row, colmap, "cost")) if "cost" in have else None
        out.append([
            _src_value(row, colmap, "sku") or name,
            name,
            _src_value(row, colmap, "category"),
            _fmt_num(cost),
            _fmt_num(price),
        ])
    return _finish(res, header, out, dropped)


def _normalize_staff(rows: list[dict], colmap: dict, res: NormalizeResult) -> NormalizeResult:
    header = list(STAFF_FIELDS)
    out: list[list] = []
    dropped = 0
    for row in rows:
        name = _src_value(row, colmap, "name")
        if not name:
            dropped += 1
            continue
        out.append([
            _src_value(row, colmap, "staff_id") or name,
            name,
            _src_value(row, colmap, "role"),
        ])
    return _finish(res, header, out, dropped)


# ── Orchestration ─────────────────────────────────────────────────────────────

def normalize_csv(store_id: int, file_type: str, content: str,
                  force_remap: bool = False) -> NormalizeResult:
    """Translate an arbitrary POS CSV into canonical CSV, learning and
    persisting the column mapping the first time a format is seen."""
    from app.core.db import SessionLocal, POSColumnMapping

    try:
        headers, rows = read_csv_text(content)
    except Exception as exc:
        return NormalizeResult(ok=False, error=f"Could not read the file as CSV ({exc}).")

    fp = header_fingerprint(headers)

    # 1. Stored mapping for this exact format
    if not force_remap:
        with SessionLocal() as db:
            stored = db.query(POSColumnMapping).filter(
                POSColumnMapping.store_id == store_id,
                POSColumnMapping.file_type == file_type,
                POSColumnMapping.header_fingerprint == fp,
            ).first()
            stored_map = dict(stored.column_map) if stored else None
        if stored_map:
            res = validate_and_normalize(rows, stored_map, file_type)
            if res.ok:
                res.mapping_source = "stored"
                return res
            logger.warning(
                "normalize: stored mapping failed validation store=%d type=%s — re-inferring",
                store_id, file_type,
            )

    # 2. Headers already canonical
    colmap = identity_mapping(headers, file_type)
    source = "identity"

    # 3. LLM inference
    if colmap is None:
        colmap = infer_mapping_llm(headers, rows, file_type)
        source = "llm"
        if colmap is None:
            return NormalizeResult(ok=False, error=(
                "Couldn't automatically understand this file's columns. "
                "Please send your POS's standard sales/menu/staff export."
            ))

    res = validate_and_normalize(rows, colmap, file_type)
    if not res.ok:
        return res
    res.mapping_source = source

    with SessionLocal() as db:
        existing = db.query(POSColumnMapping).filter(
            POSColumnMapping.store_id == store_id,
            POSColumnMapping.file_type == file_type,
            POSColumnMapping.header_fingerprint == fp,
        ).first()
        if existing:
            existing.column_map = colmap
            existing.source = source
        else:
            db.add(POSColumnMapping(
                store_id=store_id, file_type=file_type,
                header_fingerprint=fp, column_map=colmap, source=source,
            ))
        db.commit()
    logger.info(
        "normalize: learned mapping store=%d type=%s source=%s fields=%d",
        store_id, file_type, source, len(colmap),
    )
    return res


def describe_mapping_for_staff(res: NormalizeResult, type_label: str) -> str:
    """WhatsApp-friendly confirmation of what was understood."""
    lines = [f"Saved {type_label} data ({res.rows_out} rows)."]
    if res.mapping_source == "llm":
        shown = []
        for fieldname, src in res.mapped_columns.items():
            src_label = " + ".join(src) if isinstance(src, list) else src
            if _norm_header(src_label) != _norm_header(fieldname):
                shown.append(f"{src_label} → {fieldname.replace('_', ' ')}")
        if shown:
            lines.append("\nNew format detected — columns mapped automatically:")
            lines.extend(f"• {s}" for s in shown[:8])
            if len(shown) > 8:
                lines.append(f"• …and {len(shown) - 8} more")
        lines.append("\nWrong? Resend the file with caption '" + type_label + " remap'.")
    for w in res.warnings:
        lines.append(f"⚠️ {w}")
    lines.append("\nType 'summary' to run a POS audit.")
    return "\n".join(lines)
