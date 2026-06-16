"""Onboard a new café to the Revenue agent in one command.

    python -m scripts.revenue_add_cafe \
        --id sugar_rush --name "Sugar Rush" \
        --owner +923001234567 \
        --data-dir data/sugar_rush \
        --config data/sugar_rush_config.json \
        --db data/sugar_rush.db

Adds (or updates) the café in the tenants registry (REVENUE_TENANTS env or
--registry path). The owner can then message the AsaanPay bot and get advice.
"""

from __future__ import annotations

import argparse
import os

from app.revenue.tenants import Tenant, TenantRegistry, provision_cafe_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="Add a café to the Revenue agent")
    ap.add_argument("--id", required=True, help="unique cafe_id")
    ap.add_argument("--name", required=True, help="café display name")
    ap.add_argument("--owner", action="append", required=True,
                    help="owner phone (repeat for multiple owners)")
    ap.add_argument("--data-dir", default=None,
                    help="this café's private POS folder (default: data/cafes/<id>)")
    ap.add_argument("--config", default=None, help="this café's revenue config JSON")
    ap.add_argument("--db", default=None, help="this café's SQLite path")
    ap.add_argument("--registry",
                    default=os.environ.get("REVENUE_TENANTS",
                                           "data/revenue_tenants.example.json"),
                    help="tenants registry JSON to write to")
    args = ap.parse_args()

    data_dir = args.data_dir or f"data/cafes/{args.id}"

    # Create this café's OWN private folder (refuses the shared sample root).
    path, created = provision_cafe_dir(data_dir)

    registry = TenantRegistry.load(args.registry)
    # add_cafe refuses if this folder/db already belongs to another café.
    registry.add_cafe(Tenant(
        cafe_id=args.id, name=args.name, owner_phones=args.owner,
        data_dir=str(path), config_path=args.config,
        db_path=args.db or f"data/{args.id}.db",
    ))

    print(f"✅ Onboarded '{args.name}' ({args.id}) for owner(s) {', '.join(args.owner)}")
    print(f"   Private data folder {'created' if created else 'reused'}: {path}/")
    if created:
        print(f"   → Drop this café's files there: sales_detail.csv, menu.csv, staff.csv")
    print(f"   Registry {args.registry} now has {len(registry.all_tenants())} café(s).")


if __name__ == "__main__":
    main()
