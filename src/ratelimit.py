"""In-process token-bucket rate limiting.

Limits are per API process. With several processes or tasks each keeps its own
buckets, so the effective limit is multiplied by the process count; a shared
control (for example an edge/WAF rate rule) is needed once more than one
process serves traffic.
"""

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request

from src.auth import Principal, authenticate, too_many_requests

MAX_TRACKED_KEYS = 10_000


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0


class RateLimiter:
    """Named buckets of `capacity` tokens refilled over `period` seconds."""

    def __init__(
        self,
        limits: dict[str, tuple[int, float]],
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._state: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()

    def _tokens(self, bucket: str, key: str, now: float) -> float:
        capacity, period = self._limits[bucket]
        tokens, last = self._state.get((bucket, key), (float(capacity), now))
        return min(float(capacity), tokens + (now - last) * capacity / period)

    def _retry_after(self, bucket: str, tokens: float) -> int:
        capacity, period = self._limits[bucket]
        return max(1, math.ceil((1 - tokens) * period / capacity))

    def _store(self, bucket: str, key: str, tokens: float, now: float) -> None:
        self._state[(bucket, key)] = (tokens, now)
        self._state.move_to_end((bucket, key))
        while len(self._state) > self._max_keys:
            self._state.popitem(last=False)

    def consume(self, bucket: str, key: str) -> Decision:
        with self._lock:
            now = self._clock()
            tokens = self._tokens(bucket, key, now)
            if tokens < 1:
                self._store(bucket, key, tokens, now)
                return Decision(False, self._retry_after(bucket, tokens))
            self._store(bucket, key, tokens - 1, now)
            return Decision(True)

    def has_capacity(self, bucket: str, key: str) -> Decision:
        """Check without consuming (used to gate on recorded failures)."""
        with self._lock:
            tokens = self._tokens(bucket, key, self._clock())
            if tokens < 1:
                return Decision(False, self._retry_after(bucket, tokens))
            return Decision(True)


def rate_limit(bucket: str):
    """Per-user limit, applied after authentication and role checks."""

    def dependency(
        request: Request, principal: Principal = Depends(authenticate)  # noqa: B008
    ) -> None:
        limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
        if limiter is None:
            return
        decision = limiter.consume(bucket, principal.sub)
        if not decision.allowed:
            raise too_many_requests(decision.retry_after)

    return dependency
