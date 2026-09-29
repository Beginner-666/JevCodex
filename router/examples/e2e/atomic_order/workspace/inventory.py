from dataclasses import dataclass
from decimal import Decimal


@dataclass
class StockItem:
    quantity: int
    unit_price: Decimal


class Inventory:
    def __init__(self, items: dict[str, StockItem]) -> None:
        self.items = items

    def get(self, sku: str) -> StockItem | None:
        return self.items.get(sku)
