"""In-memory rate limiter used by the enrollment endpoints.

For a single-process Phase 1 deployment, a token-bucket in process memory is
sufficient. If we scale to multiple workers, move to Redis.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class TokenBucket:
    """Simple token bucket — capacity tokens, refilled at `rate` per second."""

    __slots__ = ("capacity", "rate", "tokens", "last_refill")

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity = capacity
        self.rate = rate
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def try_consume(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


_buckets: dict[tuple[str, str], TokenBucket] = defaultdict(
    lambda: TokenBucket(capacity=10.0, rate=10.0 / 60.0)
)
_lock = asyncio.Lock()


async def check_rate_limit(scope: str, key: str) -> bool:
    """Return True if the caller is under the limit. Currently 10/min/key."""
    async with _lock:
        return _buckets[(scope, key)].try_consume()


async def check_fetch_rate_limit(source_ip: str) -> bool:
    """Used by the factory installer endpoint."""
    return await check_rate_limit("fetch", source_ip)
