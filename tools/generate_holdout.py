#!/usr/bin/env python3
"""Generate a held-out dataset with a DIFFERENT bad actor and seed."""

import csv, random, hashlib
from datetime import datetime, timedelta
from pathlib import Path

random.seed(99)
OUT = Path(__file__).resolve().parent.parent / "data" / "holdout"
OUT.mkdir(parents=True, exist_ok=True)

MENU = [
    ("ESP","Espresso","Coffee",70,420),("AMR","Americano","Coffee",85,480),
    ("CAP","Cappuccino","Coffee",110,560),("LAT","Cafe Latte","Coffee",120,580),
    ("FLW","Flat White","Coffee",115,600),("MOC","Mocha","Coffee",140,650),
    ("CLB","Cold Brew","Coffee",130,680),("ICL","Iced Latte","Coffee",135,640),
    ("MAT","Matcha Latte","Coffee",190,720),("HOT","Hot Chocolate","Coffee",150,560),
    ("CRO","Butter Croissant","Bakery",180,520),("PNC","Pain au Chocolat","Bakery",220,600),
    ("CKE","Slice of Cake","Bakery",260,780),("CKE2","Cheesecake","Bakery",320,880),
    ("AVT","Avocado Toast","Food",520,1450),("BFP","Big Breakfast","Food",780,1850),
    ("SAN","Chicken Sandwich","Food",560,1350),("PAS","Pasta","Food",640,1650),
    ("SAL","Garden Salad","Food",430,1250),("IMP","Imported Soda","Other",380,480),
    ("WTR","Bottled Water","Other",60,150),
]
MENU_W = {
    "ESP":4,"AMR":6,"CAP":10,"LAT":11,"FLW":9,"MOC":5,"CLB":6,"ICL":7,"MAT":4,"HOT":3,
    "CRO":7,"PNC":4,"CKE":5,"CKE2":3,"AVT":5,"BFP":4,"SAN":5,"PAS":4,"SAL":3,"IMP":3,"WTR":4,
}
MENU_BY_SKU = {m[0]: m for m in MENU}

STAFF = [
    ("S01","Ayesha","barista"),("S02","Hamza","barista"),("S03","Bilal","server"),
    ("S04","Sara","server"),("S05","Usman","barista"),("S06","Zainab","server"),
    ("S07","Daniyal","server"),("S08","Mahnoor","barista"),
]
BAD_ACTOR = "S06"
STAFF_IDS = [s[0] for s in STAFF]

def href(x):
    return "C" + hashlib.sha1(str(x).encode()).hexdigest()[:10]

START = datetime(2026,4,27)
DAYS = 35

regulars = [href(f"reg{i}") for i in range(55)]
lapsed   = [href(f"lap{i}") for i in range(22)]
occ      = [href(f"occ{i}") for i in range(220)]
onetime_pool_size = 800
LAPSE_DAY = 18

def pick_customer(day_idx):
    r = random.random()
    if r < 0.42:
        return random.choice(regulars)
    elif r < 0.55:
        if day_idx < LAPSE_DAY:
            return random.choice(lapsed)
        return random.choice(regulars)
    elif r < 0.78:
        return random.choice(occ)
    else:
        return href(f"one{random.randint(0,onetime_pool_size)}")

def order_time(day):
    parts = [(7,11,0.30),(11,14,0.20),(14,17,0.12),(17,21,0.28),(21,23,0.10)]
    rs = random.random(); cum=0
    for sh,eh,w in parts:
        cum+=w
        if rs<=cum:
            h = random.randint(sh,eh-1); m = random.randint(0,59)
            return day.replace(hour=h, minute=m, second=random.randint(0,59))
    return day.replace(hour=10, minute=0)

def choose_items():
    n = random.choices([1,2,3], weights=[55,33,12])[0]
    return random.choices(list(MENU_W.keys()), weights=list(MENU_W.values()), k=n)

def pay_method():
    return random.choices(["card","cash","wallet","qr"], weights=[34,40,16,10])[0]

rows = []
order_counter = 0
baseline_void_p, baseline_comp_p, baseline_disc_p = 0.012, 0.010, 0.06

for d in range(DAYS):
    day = START + timedelta(days=d)
    weekend = day.weekday() >= 5
    base = 150 if weekend else 92
    n_orders = max(40, int(random.gauss(base, base*0.12)))
    for _ in range(n_orders):
        order_counter += 1
        oid = f"ORD{order_counter:06d}"
        dt = order_time(day)
        staff = random.choice(STAFF_IDS)
        staff_name = dict((s[0],s[1]) for s in STAFF)[staff]
        channel = random.choices(["dine_in","takeaway"], weights=[62,38])[0]
        table = f"T{random.randint(1,18)}" if channel=="dine_in" else ""
        method = pay_method()
        is_digital = method in ("card","wallet","qr")
        tax_rate = 0.05 if is_digital else 0.15
        cust = pick_customer(d) if (is_digital or random.random()<0.25) else ""

        is_bad = staff == BAD_ACTOR
        void_p = baseline_void_p + (0.075 if is_bad else 0)
        comp_p = baseline_comp_p + (0.05 if is_bad else 0)
        disc_p = baseline_disc_p + (0.06 if is_bad else 0)

        skus = choose_items()
        order_pay = 0.0
        order_status = "closed"
        for i, sku in enumerate(skus):
            _,name,cat,cost,price = MENU_BY_SKU[sku]
            qty = 1 if cat in ("Coffee","Bakery","Other") else random.choices([1,2],weights=[85,15])[0]
            unit = price
            disc = 0.0
            is_void = 0; void_af = 0; is_comp = 0
            if random.random() < disc_p:
                disc = round(unit*qty*random.choice([0.10,0.15,0.20]),0)
            line_amt = unit*qty - disc
            if random.random() < comp_p:
                is_comp = 1
                line_amt = 0.0
            elif random.random() < void_p:
                is_void = 1
                if is_bad and (not is_digital):
                    void_af = 1
                else:
                    void_af = 1 if random.random()<0.4 else 0
                line_amt = 0.0
                order_status = "partial_void"
            if not is_void and not is_comp:
                order_pay += line_amt
            rows.append([oid, dt.isoformat(sep=" "), staff, staff_name, table, channel,
                         sku, name, cat, qty, unit, round(line_amt,0), round(disc,0),
                         is_void, void_af, is_comp, order_status, method, None, tax_rate,
                         cust])
        pay_total = round(order_pay*(1+tax_rate),0)
        for r in rows:
            if r[0]==oid and r[18] is None:
                r[18] = pay_total

with open(OUT / "sales_detail.csv","w",newline="") as f:
    w=csv.writer(f)
    w.writerow(["order_id","datetime","staff_id","staff_name","table","channel",
                "item_sku","item_name","category","qty","unit_price","line_amount",
                "discount_amount","is_void","void_after_fire","is_comp","order_status",
                "payment_method","payment_amount","tax_rate","customer_ref"])
    w.writerows(rows)

with open(OUT / "menu.csv","w",newline="") as f:
    w=csv.writer(f); w.writerow(["sku","name","category","cost","price"])
    for sku,name,cat,cost,price in MENU: w.writerow([sku,name,cat,cost,price])

with open(OUT / "staff.csv","w",newline="") as f:
    w=csv.writer(f); w.writerow(["staff_id","name","role"])
    for s in STAFF: w.writerow(list(s))

print(f"Held-out dataset written to {OUT}")
print(f"  Bad actor: {BAD_ACTOR}")
print(f"  Rows: {len(rows)}")
print(f"  Orders: {len(set(r[0] for r in rows))}")
