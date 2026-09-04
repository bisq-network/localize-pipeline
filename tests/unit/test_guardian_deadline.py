from __future__ import annotations

import pytest

from localize.guardian.deadline import PollDeadline, PollDeadlineExceeded


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
