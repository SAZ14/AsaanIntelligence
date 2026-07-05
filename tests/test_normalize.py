"""Universal CSV normalization: tolerant parsers, mapping resolution
(stored → identity → LLM), validation gate, canonical output that the
existing loader/analytics stack can consume unchanged.
"""
import io
import csv as _csv

import pytest
from unittest.mock import patch

from app.ingest import normalize as nz
from tests.conftest import seed_chain, seed_store


# ── Tolerant value parsers ────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected_iso", [
    ("2026-06-04 14:30:00", "2026-06-04T14:30:00"),
    ("2026-06-04", "2026-06-04T00:00:00"),
    ("04/06/2026 14:30", "2026-06-04T14:30:00"),          # dd/mm/yyyy (Pakistan)
    ("04/06/2026 2:30 PM", "2026-06-04T14:30:00"),
    ("13/05/2026", "2026-05-13T00:00:00"),                 # unambiguous day-first
    ("04-Jun-2026 2:30 PM", "2026-06-04T14:30:00"),
    ("4 Jun 2026", "2026-06-04T00:00:00"),
    ("2026/06/04 14:30:00", "2026-06-04T14:30:00"),
])
def test_parse_datetime_formats(value, expected_iso):
    dt = nz.parse_datetime_value(value)
    assert dt is not None, value
    assert dt.isoformat() == expected_iso


def test_parse_datetime_epoch_and_garbage():
    assert nz.parse_datetime_value("1750000000").year == 2025
    assert nz.parse_datetime_value("") is None
    assert nz.parse_datetime_value("not a date") is None


@pytest.mark.parametrize("value,expected", [
    ("1050", 1050.0),
    ("1,050.50", 1050.5),
    ("Rs. 1,050.50", 1050.5),
    ("PKR 720", 720.0),
    ("₨950", 950.0),
    ("(500)", -500.0),
    ("16%", 16.0),
    ("", None),
    ("N/A", None),
    ("-", None),
    ("abc", None),
])
def test_parse_number_formats(value, expected):
    assert nz.parse_number_value(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("Yes", True), ("1", True), ("VOID", True), ("Cancelled", True),
    ("No", False), ("0", False), ("", False), ("Completed", False), ("Paid", False),
])
def test_parse_bool_formats(value, expected):
    assert nz.parse_bool_value(value) is expected


# ── Reading: delimiters, BOM, blank lines ────────────────────────────────────

def test_read_csv_sniffs_semicolon_and_strips_bom():
    content = "﻿name;price\nBurger;500\n\nPizza;900\n"
    headers, rows = nz.read_csv_text(content)
    assert headers == ["name", "price"]
    assert rows == [{"name": "Burger", "price": "500"}, {"name": "Pizza", "price": "900"}]


def test_header_fingerprint_ignores_case_spacing_order():
    a = nz.header_fingerprint(["Receipt No", "Item Name"])
    b = nz.header_fingerprint(["item name", "RECEIPT_NO"])
    assert a == b


# ── Identity path (canonical headers, no LLM) ────────────────────────────────

CANONICAL_SALES = (
    "order_id,datetime,staff_id,staff_name,table,channel,item_sku,item_name,"
    "category,qty,unit_price,line_amount,discount_amount,is_void,void_after_fire,"
    "is_comp,order_status,payment_method,payment_amount,tax_rate,customer_ref\n"
    "O1,2026-06-04 14:30:00,S1,Ali,5,dine-in,SKU1,Burger,Food,2,500,1000,0,false,"
    "false,false,closed,cash,1000,0.16,\n"
)


def test_identity_path_used_for_canonical_headers(two_store_ids):
    store_id = two_store_ids[0]
    with patch.object(nz, "infer_mapping_llm") as mock_llm:
        res = nz.normalize_csv(store_id, "pos_sales", CANONICAL_SALES)
    mock_llm.assert_not_called()
    assert res.ok
    assert res.mapping_source == "identity"
    assert res.rows_out == 1


# ── LLM path with realistic foreign formats ──────────────────────────────────

EPOSMATIC_STYLE = (
    "Receipt No,Date,Time,Cashier,Item Name,Qty,Rate,Amount,Discount,Status,Payment Type\n"
    'INV-001,04/06/2026,2:30 PM,Ahmed,Beefy Bypass,2,"Rs. 720","Rs. 1,440",0,Completed,Cash\n'
    'INV-001,04/06/2026,2:30 PM,Ahmed,Crispy Cure,1,"Rs. 700","Rs. 700",50,Completed,Cash\n'
    'INV-002,04/06/2026,3:10 PM,Sara,Pulse Pounder,1,"Rs. 950","Rs. 950",0,Cancelled,Card\n'
)

EPOSMATIC_MAPPING = {
    "order_id": "Receipt No",
    "datetime": ["Date", "Time"],
    "staff_name": "Cashier",
    "item_name": "Item Name",
    "qty": "Qty",
    "unit_price": "Rate",
    "line_amount": "Amount",
    "discount_amount": "Discount",
    "order_status": "Status",
    "payment_method": "Payment Type",
}


@pytest.fixture
def two_store_ids():
    chain = seed_chain("NZ Chain")
    return (seed_store(chain, name="NZ Store A"), seed_store(chain, name="NZ Store B"))


def test_llm_path_maps_learns_and_reuses(two_store_ids):
    store_id = two_store_ids[0]
    with patch.object(nz, "infer_mapping_llm", return_value=EPOSMATIC_MAPPING) as mock_llm:
        res = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    assert res.ok, res.error
    assert res.mapping_source == "llm"
    mock_llm.assert_called_once()
    assert res.rows_out == 3

    rows = list(_csv.DictReader(io.StringIO(res.canonical_csv)))
    assert rows[0]["order_id"] == "INV-001"
    assert rows[0]["datetime"] == "2026-06-04 14:30:00"   # split Date+Time joined, day-first
    assert rows[0]["unit_price"] == "720"                  # Rs. stripped
    assert rows[0]["line_amount"] == "1440"                # thousands comma stripped
    assert rows[0]["is_void"] == "false"
    assert rows[2]["is_void"] == "true"                    # derived from Status=Cancelled
    # payment_amount derived from line sums when no paid-total column
    assert float(rows[0]["payment_amount"]) == pytest.approx(1440 + 700 - 50)
    assert any("paid-total" in w.lower() for w in res.warnings)

    # Second upload of the same format: stored mapping, LLM never called
    with patch.object(nz, "infer_mapping_llm") as mock_llm2:
        res2 = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    mock_llm2.assert_not_called()
    assert res2.ok
    assert res2.mapping_source == "stored"


def test_canonical_output_feeds_existing_loader_and_analytics(two_store_ids):
    """The whole point: normalized output must flow through the untouched
    downstream stack (rows_to_orders + integrity analytics)."""
    store_id = two_store_ids[0]
    with patch.object(nz, "infer_mapping_llm", return_value=EPOSMATIC_MAPPING):
        res = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    assert res.ok

    from app.ingest.loader import rows_to_orders
    from app.ingest.mappings import cafe_generic
    orders = rows_to_orders(list(_csv.DictReader(io.StringIO(res.canonical_csv))),
                            cafe_generic.SALES_DETAIL)
    assert len(orders) == 2  # INV-001 (2 lines) + INV-002
    inv1 = next(o for o in orders if o.order_id == "INV-001")
    assert len(inv1.line_items) == 2
    assert inv1.datetime.year == 2026 and inv1.datetime.day == 4 and inv1.datetime.month == 6
    inv2 = next(o for o in orders if o.order_id == "INV-002")
    assert inv2.line_items[0].is_void is True


IPOS_STYLE = (
    "Bill#|BillDate|Waiter Code|Waiter|Product|Quantity|Price|Net Amount|Void|Tender\n"
    "B-501|04-Jun-2026 14:05|W2|Bilal|Prescription Patty|1|670|670|No|CASH\n"
    "B-502|04-Jun-2026 14:22|W2|Bilal|Cardiac Crisis|2|1250|2500|No|CARD\n"
)

IPOS_MAPPING = {
    "order_id": "Bill#",
    "datetime": "BillDate",
    "staff_id": "Waiter Code",
    "staff_name": "Waiter",
    "item_name": "Product",
    "qty": "Quantity",
    "unit_price": "Price",
    "line_amount": "Net Amount",
    "is_void": "Void",
    "payment_method": "Tender",
}


def test_pipe_delimited_ipos_style(two_store_ids):
    store_id = two_store_ids[1]
    with patch.object(nz, "infer_mapping_llm", return_value=IPOS_MAPPING):
        res = nz.normalize_csv(store_id, "pos_sales", IPOS_STYLE)
    assert res.ok, res.error
    rows = list(_csv.DictReader(io.StringIO(res.canonical_csv)))
    assert rows[0]["datetime"] == "2026-06-04 14:05:00"
    assert rows[0]["staff_id"] == "W2"
    assert rows[1]["line_amount"] == "2500"


# ── Validation gate: bad mappings are rejected, never stored ─────────────────

def test_wrong_mapping_rejected_and_not_saved(two_store_ids):
    store_id = two_store_ids[0]
    bad = dict(EPOSMATIC_MAPPING, datetime="Payment Type")  # dates won't parse
    with patch.object(nz, "infer_mapping_llm", return_value=bad):
        res = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    assert not res.ok
    assert "rows parsed" in res.error or "No rows" in res.error

    from app.core.db import SessionLocal, POSColumnMapping
    with SessionLocal() as db:
        assert db.query(POSColumnMapping).filter(
            POSColumnMapping.store_id == store_id
        ).count() == 0


def test_missing_critical_column_clear_error(two_store_ids):
    store_id = two_store_ids[0]
    no_order_id = {k: v for k, v in EPOSMATIC_MAPPING.items() if k != "order_id"}
    with patch.object(nz, "infer_mapping_llm", return_value=no_order_id):
        res = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    assert not res.ok
    assert "order_id" in res.error


def test_llm_total_failure_gives_actionable_error(two_store_ids):
    store_id = two_store_ids[0]
    with patch.object(nz, "infer_mapping_llm", return_value=None):
        res = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    assert not res.ok
    assert "columns" in res.error.lower()


def test_remap_forces_reinference(two_store_ids):
    store_id = two_store_ids[1]
    with patch.object(nz, "infer_mapping_llm", return_value=IPOS_MAPPING) as m1:
        assert nz.normalize_csv(store_id, "pos_sales", IPOS_STYLE).ok
    with patch.object(nz, "infer_mapping_llm", return_value=IPOS_MAPPING) as m2:
        res = nz.normalize_csv(store_id, "pos_sales", IPOS_STYLE, force_remap=True)
    assert res.ok
    m2.assert_called_once()


# ── Menu and staff files ──────────────────────────────────────────────────────

def test_menu_foreign_format(two_store_ids):
    store_id = two_store_ids[0]
    content = "Item Code,Description,Group,Sale Price\nP1,Beefy Bypass,Burgers,\"Rs. 720\"\n"
    mapping = {"sku": "Item Code", "name": "Description",
               "category": "Group", "price": "Sale Price"}
    with patch.object(nz, "infer_mapping_llm", return_value=mapping):
        res = nz.normalize_csv(store_id, "pos_menu", content)
    assert res.ok, res.error
    rows = list(_csv.DictReader(io.StringIO(res.canonical_csv)))
    assert rows[0] == {"sku": "P1", "name": "Beefy Bypass", "category": "Burgers",
                       "cost": "", "price": "720"}
    assert any("cost" in w.lower() for w in res.warnings)


def test_staff_minimal_format(two_store_ids):
    store_id = two_store_ids[0]
    content = "Employee Name,Designation\nAhmed Khan,Cashier\nSara Malik,Waiter\n"
    mapping = {"name": "Employee Name", "role": "Designation"}
    with patch.object(nz, "infer_mapping_llm", return_value=mapping):
        res = nz.normalize_csv(store_id, "pos_staff", content)
    assert res.ok
    rows = list(_csv.DictReader(io.StringIO(res.canonical_csv)))
    assert rows[0]["staff_id"] == "Ahmed Khan"  # defaults to name when absent
    assert rows[1]["role"] == "Waiter"


# ── Staff-facing message ──────────────────────────────────────────────────────

def test_describe_mapping_message(two_store_ids):
    store_id = two_store_ids[0]
    with patch.object(nz, "infer_mapping_llm", return_value=EPOSMATIC_MAPPING):
        res = nz.normalize_csv(store_id, "pos_sales", EPOSMATIC_STYLE)
    msg = nz.describe_mapping_for_staff(res, "sales")
    assert "Saved sales data (3 rows)" in msg
    assert "Receipt No → order id" in msg
    assert "remap" in msg
