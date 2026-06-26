"""CSV / flat-file POS connector.

The reference connector and the one used in tests. Many small POS systems (and
every POS that can export a sales-detail report) can produce CSV files; this
connector reads them through the existing config-driven loader, so a new export
format is handled by pointing ``mapping`` at the right field map.

Config ``connection`` keys (paths, absolute or relative to ``base_dir``):

    base_dir : optional directory the three filenames are resolved against
    sales    : sales-detail export        (default "sales_detail.csv")
    menu     : menu / product export      (default "menu.csv")
    staff    : staff / employee export    (default "staff.csv")
"""

from __future__ import annotations

from pathlib import Path

from app.ingest import loader
from app.pos.base import POSConnector, POSData, RestaurantConfig, _load_mapping, register_connector


class CSVPOSConnector(POSConnector):
    def __init__(self, config: RestaurantConfig) -> None:
        super().__init__(config)
        conn = config.connection
        base = Path(conn.get("base_dir", "."))
        self.sales_path = base / conn.get("sales", "sales_detail.csv")
        self.menu_path = base / conn.get("menu", "menu.csv")
        self.staff_path = base / conn.get("staff", "staff.csv")
        self.mapping = _load_mapping(config.mapping)

    def fetch(self) -> POSData:
        for label, p in (
            ("sales", self.sales_path),
            ("menu", self.menu_path),
            ("staff", self.staff_path),
        ):
            if not p.exists():
                raise FileNotFoundError(
                    f"{self.config.venue_name}: {label} export not found at {p}"
                )

        orders = loader.load_orders(self.sales_path, self.mapping.SALES_DETAIL)
        menu = loader.load_menu(self.menu_path, self.mapping.MENU)
        staff = loader.load_staff(self.staff_path, self.mapping.STAFF)
        return POSData(orders=orders, menu=menu, staff=staff)


register_connector("csv", CSVPOSConnector)

