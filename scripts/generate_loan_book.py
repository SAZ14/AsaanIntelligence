"""Generate the synthetic demo customer book for the loans agent.

Produces a seeded, reproducible mix of archetypes so the demo shows every
branch of the policy: strong files under genuine stress (offer), liquid
strong files (keep pre-approved), thin files (monitor), and poor files with
bounces / eCIB flags (decline with secured alternatives).

Usage:
    python scripts/generate_loan_book.py [--n 60] [--seed 47] [--out path.json]
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.loans.book import DEFAULT_BOOK_PATH, save_book  # noqa: E402
from app.agents.loans.models import CustomerProfile, Obligation  # noqa: E402

FIRST_NAMES = [
    "Ahmed", "Ali", "Usman", "Bilal", "Hamza", "Zeeshan", "Kashif", "Imran",
    "Faisal", "Rehan", "Sohail", "Adnan", "Mudassir", "Junaid", "Tariq",
    "Ayesha", "Fatima", "Zainab", "Maryam", "Hira", "Sana", "Nimra",
    "Rabia", "Amna", "Khadija", "Mahnoor", "Iqra", "Bushra",
]
LAST_NAMES = [
    "Khan", "Malik", "Sheikh", "Butt", "Chaudhry", "Qureshi", "Siddiqui",
    "Ansari", "Baloch", "Jatoi", "Memon", "Shah", "Durrani", "Gondal",
    "Bajwa", "Awan", "Abbasi", "Rajput", "Soomro", "Bhatti",
]
BANKS = [
    ("Habib Bank Limited", "HBL"), ("United Bank Limited", "UBL"),
    ("MCB Bank", "MCB"), ("National Bank of Pakistan", "NBP"),
    ("Meezan Bank", "MEBL"), ("Bank Alfalah", "BAFL"),
    ("Allied Bank", "ABL"), ("Sindh Bank", "SNDB"),
    ("Bank of Punjab", "BOP"), ("HBL Microfinance Bank", "HBLMFB"),
]
CITIES = [
    "Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan",
    "Hyderabad", "Peshawar", "Quetta", "Sialkot", "Gujranwala", "Okara",
    "Jhelum", "Badin", "Abbottabad", "Sargodha",
]
EMPLOYMENTS = [
    ("Careem captain (gig)", 45_000, 110_000),
    ("Foodpanda rider (gig)", 35_000, 80_000),
    ("Private school teacher (contract)", 40_000, 130_000),
    ("Nishat Mills (permanent)", 45_000, 150_000),
    ("Sindh Health Department (government)", 80_000, 250_000),
    ("Punjab Police (government)", 60_000, 160_000),
    ("Kiryana store owner (self-employed)", 50_000, 200_000),
    ("Tailoring business (self-employed)", 40_000, 120_000),
    ("Software house QA analyst (permanent)", 90_000, 300_000),
    ("Bank teller (permanent)", 55_000, 140_000),
    ("Pharmaceutical sales rep (permanent)", 70_000, 180_000),
    ("Auto workshop mechanic (self-employed)", 40_000, 100_000),
]
OBLIGATION_KINDS = [
    ("motorcycle instalment", 2_000, 8_000),
    ("existing personal loan EMI", 4_000, 25_000),
    ("committee (BC) contribution", 3_000, 15_000),
    ("car instalment", 15_000, 45_000),
    ("appliance instalment", 1_500, 6_000),
]

# (archetype, weight): drives score band × liquidity so every policy branch
# appears in a 60-customer book.
ARCHETYPES = [
    ("strong_stressed", 5),
    ("strong_liquid", 4),
    ("acceptable_stressed", 4),
    ("acceptable_liquid", 3),
    ("thin", 2),
    ("poor_stressed", 2),
    ("poor_liquid", 1),
]


def _balances(rng: random.Random, avg: int, stressed: bool) -> list[int]:
    """Six end-of-month balances, oldest first, consistent with liquidity."""
    if stressed:
        start = int(avg * rng.uniform(1.2, 2.2))
        end = int(avg * rng.uniform(0.05, 0.35))
        pts = [start + (end - start) * i / 5 for i in range(6)]
    else:
        drift = rng.uniform(0.9, 1.4)
        pts = [avg * rng.uniform(0.8, 1.2) * (1 + (drift - 1) * i / 5) for i in range(6)]
    return [int(round(p, -2)) for p in pts]


def make_customer(rng: random.Random, idx: int, archetype: str) -> CustomerProfile:
    band, _, liquidity = archetype.partition("_")
    stressed = liquidity == "stressed" or (band == "poor" and liquidity != "liquid")

    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    bank, code = rng.choice(BANKS)
    employment, lo, hi = rng.choice(EMPLOYMENTS)
    income = int(round(rng.randint(lo, hi), -3))

    if band == "strong":
        account_age = round(rng.uniform(3.5, 15.0), 1)
        salary_months = rng.choice([11, 12, 12])
        avg_balance = int(round(rng.randint(60_000, 900_000), -2))
        previous_loan = rng.choice(["clean", "clean", "late_1_2"])
        bounce, dpd, writeoff = False, False, False
    elif band == "acceptable":
        account_age = round(rng.uniform(1.2, 9.0), 1)
        salary_months = rng.choice([9, 10, 11])
        avg_balance = int(round(rng.randint(16_000, 200_000), -2))
        previous_loan = rng.choice(["clean", "late_1_2", "none"])
        bounce, dpd, writeoff = False, False, False
    elif band == "thin":
        account_age = round(rng.uniform(0.2, 2.5), 1)
        salary_months = rng.choice([6, 7, 8, 9])
        avg_balance = int(round(rng.randint(3_000, 40_000), -2))
        previous_loan = "none"
        bounce, dpd, writeoff = False, False, False
    else:  # poor — at least one heavy negative flag
        account_age = round(rng.uniform(0.5, 12.0), 1)
        salary_months = rng.choice([8, 9, 10, 11])
        avg_balance = int(round(rng.randint(10_000, 800_000), -2))
        previous_loan = rng.choice(["late_1_2", "none"])
        bounce = rng.random() < 0.7
        dpd = rng.random() < 0.5
        writeoff = rng.random() < 0.4
        if not (bounce or dpd or writeoff):
            bounce = True

    n_obl = rng.choices([0, 1, 2, 3], weights=[3, 4, 2, 1])[0]
    obligations = []
    for kind, olo, ohi in rng.sample(OBLIGATION_KINDS, n_obl):
        cap = max(olo, min(ohi, int(income * 0.15)))
        obligations.append(Obligation(kind, int(round(rng.randint(olo, cap), -2))
                                      if cap > olo else olo))

    days_below = rng.randint(12, 27) if stressed else rng.randint(0, 7)
    gap = rng.randint(6, 18) if stressed else None

    return CustomerProfile(
        customer_id=f"C{idx:03d}",
        name=name,
        age=rng.randint(21, 60),
        bank=bank,
        bank_code=code,
        city=rng.choice(CITIES),
        employment=employment,
        net_monthly_income=income,
        account_age_years=account_age,
        salary_months_12=salary_months,
        avg_balance_6m=avg_balance,
        previous_loan=previous_loan,
        cheque_bounce_12m=bounce,
        ecib_dpd90_24m=dpd,
        ecib_writeoff=writeoff,
        obligations=obligations,
        eom_balances=_balances(rng, avg_balance, stressed),
        days_below_5k_30d=days_below,
        salary_to_low_gap_days=gap,
    )


def generate(n: int = 60, seed: int = 47) -> list[CustomerProfile]:
    rng = random.Random(seed)
    names, weights = zip(*ARCHETYPES)
    archetypes = rng.choices(names, weights=weights, k=n)
    return [make_customer(rng, i + 1, a) for i, a in enumerate(archetypes)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=47)
    ap.add_argument("--out", type=Path, default=DEFAULT_BOOK_PATH)
    args = ap.parse_args()

    customers = generate(args.n, args.seed)
    path = save_book(customers, args.out)

    from app.agents.loans.policy import triage
    queues = triage(customers)
    print(f"Wrote {len(customers)} customers to {path}")
    print(f"  OFFER queue:   {len(queues['OFFER'])}")
    print(f"  MONITOR:       {len(queues['MONITOR'])}")
    print(f"  DECLINE:       {len(queues['DECLINE'])}")


if __name__ == "__main__":
    main()
