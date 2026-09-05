"""One monotonic wall-clock budget shared by a Guardian poll."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import httpx


_MAX_HTTP_PHASE_SLICE_SECONDS = 5.0


class PollDeadlineExceeded(RuntimeError):
    """The current poll exhausted its wall-clock budget."""


class PollDeadline:
    """Clamp blocking operations to one absolute monotonic deadline."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("poll deadline must be a positive finite number")
        self._clock = clock
        self._expires_at = clock() + float(timeout_seconds)

    def remaining(self, operation_limit: float | None = None) -> float:
        """Return positive remaining seconds, optionally capped per operation."""

        remaining = self._expires_at - self._clock()
        if remaining <= 0:
            raise PollDeadlineExceeded("Guardian poll deadline was exceeded.")
        if operation_limit is None:
            return remaining
        if (
            isinstance(operation_limit, bool)
            or not isinstance(operation_limit, (int, float))
            or not math.isfinite(operation_limit)
            or operation_limit <= 0
        ):
            raise ValueError("operation timeout must be a positive finite number")
        return min(float(operation_limit), remaining)

    def require_remaining(self) -> None:
        """Fail once the absolute deadline has elapsed."""

        self.remaining()


def deadline_httpx_timeout(
    deadline: PollDeadline | None,
    configured: float | httpx.Timeout,
) -> httpx.Timeout:
    """Bound each synchronous HTTP phase within one poll deadline.

    HTTPX applies pool/connect/write/read limits independently. When a poll
    deadline exists, reserve at most one quarter of the current remainder for
    each phase and cap every blocking phase at five seconds. Streaming callers
    still recheck the absolute deadline after every received chunk.
    """

    timeout = configured if isinstance(configured, httpx.Timeout) else httpx.Timeout(configured)
    if deadline is None:
        return timeout
    phase_budget = min(
        _MAX_HTTP_PHASE_SLICE_SECONDS,
        deadline.remaining() / 4,
    )

    def clamp(value: float | None) -> float:
        return phase_budget if value is None else min(float(value), phase_budget)

    return httpx.Timeout(
        connect=clamp(timeout.connect),
        read=clamp(timeout.read),
        write=clamp(timeout.write),
        pool=clamp(timeout.pool),
    )


__all__ = ("PollDeadline", "PollDeadlineExceeded", "deadline_httpx_timeout")
