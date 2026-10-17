"""Per-principal token-bucket rate limiting.

API Gateway enforces coarse per-API-key quotas at the edge; this bucket is the
per-principal second layer inside each Cloud Run instance. It is approximate
across instances, which is fine because max instances is capped (free tier).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from cachetools import TTLCache


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self._rate = per_minute / 60.0
        self._capacity = float(burst or max(per_minute // 4, 5))
        self._buckets: TTLCache[str, _Bucket] = TTLCache(maxsize=10_000, ttl=600)

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key) or _Bucket(tokens=self._capacity, updated=now)
        bucket.tokens = min(self._capacity, bucket.tokens + (now - bucket.updated) * self._rate)
        bucket.updated = now
        allowed = bucket.tokens >= cost
        if allowed:
            bucket.tokens -= cost
        self._buckets[key] = bucket
        return allowed
