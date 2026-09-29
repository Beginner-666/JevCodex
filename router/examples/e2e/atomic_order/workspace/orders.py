from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from inventory import Inventory


class InvalidOrder(ValueError):
    pass


class UnknownSku(InvalidOrder):
    pass


class InsufficientStock(InvalidOrder):
    pass


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class Receipt:
    quantities: dict[str, int]
    total: Decimal


class OrderService:
    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory

    def place_order(self, lines: Iterable[OrderLine]) -> Receipt:
        """Validate and commit an order atomically.

        Quantities must be positive integers. Combine duplicate SKUs before checking
        stock. Unknown SKUs and insufficient aggregate stock are errors. On every
        error, inventory quantities must remain unchanged. A successful receipt has
        one quantity per SKU and the exact Decimal total, then inventory is reduced.
        Empty orders are invalid.
        """
        raise NotImplementedError
