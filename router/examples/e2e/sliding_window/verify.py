import importlib.util
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("candidate", workspace / "rate_limiter.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

limiter = module.SlidingWindowLimiter(2, 10.0)
assert limiter.allow("a", 0.0)
assert limiter.allow("a", 1.0)
assert not limiter.allow("a", 2.0)
assert not limiter.allow("a", 9.999)
assert limiter.allow("a", 10.0), "timestamp at now-window must expire"
assert not limiter.allow("a", 10.5)
assert limiter.allow("b", 10.5), "keys must be independent"
assert limiter.allow("b", 10.6)
assert not limiter.allow("b", 10.7)
assert limiter.allow("a", 11.0)
print("10 hidden checks passed")
