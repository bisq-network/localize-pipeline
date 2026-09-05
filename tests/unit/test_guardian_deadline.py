from __future__ import annotations

import pytest

from localize.guardian.deadline import (
    PollDeadline,
    PollDeadlineExceeded,
    deadline_httpx_timeout,
)


def test_poll_deadline_clamps_operations_to_one_absolute_budget() -> None:
    now = [10.0]
    deadline = PollDeadline(5.0, clock=lambda: now[0])

    assert deadline.remaining(30.0) == 5.0
    now[0] = 13.5
    assert deadline.remaining(30.0) == 1.5
    assert deadline.remaining(1.0) == 1.0

    now[0] = 15.0
    with pytest.raises(PollDeadlineExceeded, match="deadline"):
        deadline.require_remaining()


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
def test_poll_deadline_rejects_non_positive_or_non_finite_budgets(
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        PollDeadline(timeout)  # type: ignore[arg-type]


def test_http_phases_share_the_remaining_deadline_budget() -> None:
    deadline = PollDeadline(5.0, clock=lambda: 10.0)

    timeout = deadline_httpx_timeout(deadline, 30.0)

    assert timeout.pool == 1.25
    assert timeout.connect == 1.25
    assert timeout.write == 1.25
    assert timeout.read == 1.25


def test_http_phase_slices_preserve_tighter_operation_limits() -> None:
    deadline = PollDeadline(60.0, clock=lambda: 10.0)

    timeout = deadline_httpx_timeout(deadline, 1.0)

    assert timeout.pool == 1.0
    assert timeout.connect == 1.0
    assert timeout.write == 1.0
    assert timeout.read == 1.0


def test_http_phase_slices_have_a_bounded_inflight_grace() -> None:
    deadline = PollDeadline(60.0, clock=lambda: 10.0)

    timeout = deadline_httpx_timeout(deadline, 30.0)

    assert timeout.pool == 5.0
    assert timeout.connect == 5.0
    assert timeout.write == 5.0
    assert timeout.read == 5.0
