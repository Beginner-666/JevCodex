import importlib.util
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("candidate", workspace / "text_utils.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

cases = {
    " Hello, World! ": "hello-world",
    "already--slugged": "already-slugged",
    "A__B  C": "a-b-c",
    "***": "",
    "Café number 42": "caf-number-42",
    "-edge-": "edge",
}
for value, expected in cases.items():
    actual = module.slugify(value)
    assert actual == expected, (value, expected, actual)
print("6 hidden checks passed")
