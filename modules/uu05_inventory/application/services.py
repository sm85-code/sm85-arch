"""UU05 inventory application services: catalog, movements, adjustments, reports."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.public_service import SiabumdesPublicService
from modules.uu05_inventory.application.catalog import (
    DEFAULT_CATEGORIES,
    InventoryCatalogMixin,
    UU05_CODE,
)
from modules.uu05_inventory.application.stock_mixin import InventoryStockMixin
from modules.uu05_inventory.application.adjust_reports_mixin import InventoryAdjustReportsMixin

__all__ = ["InventoryService", "UU05_CODE", "DEFAULT_CATEGORIES"]


class InventoryService(
    InventoryCatalogMixin,
    InventoryStockMixin,
    InventoryAdjustReportsMixin,
):
    def __init__(
        self,
        session: AsyncSession,
        finance: Optional[SiabumdesPublicService] = None,
    ) -> None:
        self.session = session
        self.finance = finance or SiabumdesPublicService(session)
