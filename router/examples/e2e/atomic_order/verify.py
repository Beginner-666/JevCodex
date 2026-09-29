from decimal import Decimal
import importlib
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
sys.path.insert(0, str(workspace))
inventory_module = importlib.import_module("inventory")
orders = importlib.import_module("orders")


def make_service():
    inventory = inventory_module.Inventory(
        {
            "A": inventory_module.StockItem(5, Decimal("1.25")),
            "B": inventory_module.StockItem(2, Decimal("3.40")),
        }
    )
    return inventory, orders.OrderService(inventory)


inventory, service = make_service()
receipt = service.place_order(
    [orders.OrderLine("A", 2), orders.OrderLine("B", 1), orders.OrderLine("A", 1)]
)
assert receipt.quantities == {"A": 3, "B": 1}
assert receipt.total == Decimal("7.15")
assert (inventory.items["A"].quantity, inventory.items["B"].quantity) == (2, 1)

for bad_lines, error in (
    ([orders.OrderLine("A", 3), orders.OrderLine("A", 3)], orders.InsufficientStock),
    ([orders.OrderLine("A", 1), orders.OrderLine("X", 1)], orders.UnknownSku),
    ([orders.OrderLine("A", 0)], orders.InvalidOrder),
    ([], orders.InvalidOrder),
):
    inventory, service = make_service()
    before = {key: item.quantity for key, item in inventory.items.items()}
    try:
        service.place_order(bad_lines)
    except error:
        pass
    else:
        raise AssertionError(f"expected {error.__name__}")
    after = {key: item.quantity for key, item in inventory.items.items()}
    assert after == before, "failed order mutated inventory"
print("9 hidden checks passed")
