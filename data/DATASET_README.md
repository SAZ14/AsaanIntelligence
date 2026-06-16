# Synthetic Café Dataset — Data Dictionary & Answer Key

This is a fake but realistic dataset for a fancy Islamabad specialty café ("Roastery"-style), generated for building and testing the audit tool. It stands in for a real venue export until you have one. **Patterns are deliberately planted** so you can confirm your detection engine actually finds them — the expected figures are below.

Regenerate or tweak anytime with `generate_data.py` (seed is fixed at 42, so output is identical each run).

## The venue (at a glance)
- **Period:** 35 days (2026-04-27 → 2026-05-31)
- **Orders:** 3,795 · **Line rows:** 5,986
- **Revenue:** ~PKR 4.97M over the period (~PKR 4.26M / month)
- **Customers referenced:** 710 unique (via `customer_ref`)

## Files
**`sales_detail.csv`** — the raw export, one row per line item. Order-level fields (`payment_*`, `order_status`, `customer_ref`, `tax_rate`) repeat across that order's rows, exactly like a real POS sales-detail dump.

| column | meaning |
|---|---|
| order_id | groups line rows into one order |
| datetime | order timestamp |
| staff_id / staff_name | who rang it |
| table / channel | table no.; dine_in or takeaway |
| item_sku / item_name / category | the item |
| qty / unit_price / line_amount | quantities and money (line_amount is post-discount; 0 if voided/comped) |
| discount_amount | manual discount on the line |
| is_void / void_after_fire / is_comp | adjustment flags (void_after_fire = voided after sent to kitchen) |
| order_status | closed / partial_void |
| payment_method | cash / card / wallet / qr |
| payment_amount | total collected for the order, incl. tax (repeated on each row) |
| tax_rate | 0.05 for digital, 0.15 for cash (the ICT lever) |
| customer_ref | hashed card/loyalty id; blank for anonymous cash |

**`menu.csv`** — sku, name, category, **cost**, price. The `cost` column is what turns leakage and item analysis into real rupees.

**`staff.csv`** — staff_id, name, role.

---

## ANSWER KEY — what your engine should detect

### Leakage (≈ PKR 61,000 / month)
The planted offender is **staff `S03` (Bilal)**. He should stand out clearly against the other seven staff:
- **Suspected theft voids:** ~PKR 18,900 over the period — voided lines that are `void_after_fire = 1` **and** on **cash** orders **and** rung by S03. This is the serve-collect-cash-void signature; it should be your highest-confidence flag.
- **Excess comps:** ~PKR 45,890 over the period — comps concentrated on S03 well above the ~1% staff baseline.
- **Excess discounts:** ~PKR 6,850 over the period — discount abuse by S03.
- **Totals:** ~PKR 71,650 over 35 days → **~PKR 61,400 / month.** Baseline void/comp/discount rates for honest staff are ~1.2% / 1.0% / 6%; S03 runs far above all three.

A correct engine ranks S03 worst on integrity score and produces a monthly leakage estimate in the ~PKR 55–65k range.

### Retention (the café story)
- **55 regulars** — active across the whole period, high visit frequency. Your repeat-rate metric should be high.
- **22 lapsed regulars** — frequent for the first 18 days, then **stop completely** (`lapse_after_day = 18`). These are the win-back targets; your engine should isolate customers who were frequent early and absent in the final ~2 weeks, and estimate their lost value.
- The rest are occasional or one-time. Cash-only customers are mostly anonymous (blank `customer_ref`), so retention is measured over carded/wallet customers — note this caveat in the report.

### Operations
- **Busy:** mornings (07–11) and evenings (17–21); **dead:** weekday afternoons (14–17). Weekends ~60% busier than weekdays.
- **Digital share ≈ 60%** (card/wallet/qr at 5% tax) vs **~40% cash** (15% tax) — and the leakage hides in the cash transactions.
- **Top margin:** coffee drinks (cost ~PKR 70–190 against PKR 420–720 prices). **Low-margin "dog":** `IMP` Imported Soda (PKR 380 cost / PKR 480 price) — your top/worst-by-margin analysis should surface it.

---

*If your detection engine reproduces the figures above from `sales_detail.csv` + `menu.csv`, it works. Then point it at a real venue export and the same numbers become real.*
