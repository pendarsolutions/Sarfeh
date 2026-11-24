"""In-memory rate limits. Each mirror keeps its own: a client is limited per mirror, which is enough to keep one
installation (or one address) from taking more than its share."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class Limiter:
    """At most `n` events per `per` seconds for each key."""

    def __init__(self, n: int, per: float):
        self.n, self.per, self.events = n, per, defaultdict(deque)

    def allow(self, key: str) -> bool:
        q, now = self.events[key], time.monotonic()
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True
