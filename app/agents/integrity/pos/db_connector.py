"""POS connector that reads CSV data uploaded via WhatsApp and stored in the DB."""
from __future__ import annotations

import csv
import io
import logging

from app.agents.integrity.pos.base import (
    POSConnector, POSData, RestaurantConfig, _load_mapping, register_connector,
)

logger = logging.getLogger(__name__)


class DBCSVConnector(POSConnector):
    """Reads POS CSV data from UploadedFile rows rather than from the filesystem.

    config.connection must contain {"store_id": <int>}.
    """

    def fetch(self) -> POSData:
        from app.core.db import SessionLocal, UploadedFile
        from app.ingest.loader import row_to_menu_item, row_to_staff, rows_to_orders

        store_id: int = self.config.connection["store_id"]
        mapping = _load_mapping(self.config.mapping)

        with SessionLocal() as db:
            rows = (
                db.query(UploadedFile)
                .filter(UploadedFile.store_id == store_id)
                .all()
            )
            files: dict[str, str] = {r.file_type: r.content for r in rows}

        available = sorted(files)
        logger.info("integrity.db_connector: store=%d files=%s", store_id, available)

        if not files:
            raise FileNotFoundError(
                f"No CSV files uploaded yet for store {store_id}. "
                "Send a CSV file via WhatsApp with caption 'sales', 'menu', or 'staff'."
            )

        orders = []
        if "pos_sales" in files:
            reader = csv.DictReader(io.StringIO(files["pos_sales"]))
            orders = rows_to_orders(list(reader), mapping.SALES_DETAIL)
            logger.info("integrity.db_connector: store=%d orders_parsed=%d", store_id, len(orders))

        menu = {}
        if "pos_menu" in files:
            reader = csv.DictReader(io.StringIO(files["pos_menu"]))
            items = [row_to_menu_item(row, mapping.MENU) for row in reader]
            menu = {item.sku: item for item in items}
            logger.info("integrity.db_connector: store=%d menu_items=%d", store_id, len(menu))

        staff = {}
        if "pos_staff" in files:
            reader = csv.DictReader(io.StringIO(files["pos_staff"]))
            staff_list = [row_to_staff(row, mapping.STAFF) for row in reader]
            staff = {s.staff_id: s for s in staff_list}
            logger.info("integrity.db_connector: store=%d staff_count=%d", store_id, len(staff))

        return POSData(orders=orders, menu=menu, staff=staff)


register_connector("whatsapp_csv", DBCSVConnector)
