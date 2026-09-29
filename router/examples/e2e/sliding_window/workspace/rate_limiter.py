from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Per-key sliding-window rate limiter.

    A call to allow(key, now) succeeds when fewer than ``capacity`` successful calls
    for that key have timestamps in the half-open interval ``(now-window, now]``.
    Successful calls are recorded; rejected calls are not. Calls for different keys
    are independent. ``now`` is monotonic for each key.
    """

    def __init__(self, capacity: int, window: float) -> None:
        if capacity <= 0 or window <= 0:
            raise ValueError("capacity and window must be positive")
        self.capacity = capacity
        self.window = window
        self._events = defaultdict(deque)

    def allow(self, key: str, now: float) -> bool:
        raise NotImplementedError
