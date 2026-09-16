"""A failed prevention draft must not withhold an authorized translation fix."""

from dataclasses import replace

import pytest

from localize.guardian.models import GuardianMode
from localize.guardian.prevention_runtime import PreventionBatchOutcome
from localize.guardian.state import GuardianState
from tests.unit.test_guardian_controller import (
    COMMIT_SHA, FakePreventionRunner, RecurrenceCodexDriver, _config,
    _controller, _policy, _prevention_policy, _pull, _snapshot, runtime,
)

controller_runtime = runtime


@pytest.mark.parametrize("failure", [False, True])
def test_translation_publishes_once_while_prevention_retries(
    tmp_path, controller_runtime, failure,
):
    _base, _head, checkout, provider, broker, sequence = controller_runtime
    policy = replace(_policy(), prevention=_prevention_policy())
    prevention = FakePreventionRunner(
        result=PreventionBatchOutcome(
            deferred=0 if failure else 1,
            failures=("WorkspaceError",) if failure else (),
        ), sequence=sequence,
    )
    driver = RecurrenceCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,)),
            checkout=checkout, provider=provider, broker=broker, driver=driver,
            prevention_runner=prevention,
        )
        first = controller.poll_once()
        assert first.applied_commits == (COMMIT_SHA,)
        assert state.pending_event_revisions(mode=GuardianMode.PROPOSE_PREVENTION)
        assert state.status_snapshot(mode=GuardianMode.PROPOSE_PREVENTION).pending_revisions > 0
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)
        second = controller.poll_once()
        assert second.applied_commits == ()
        assert len(prevention.propose_calls) == 2
        prevention.result = PreventionBatchOutcome()
        third = controller.poll_once()
        fourth = controller.poll_once()
        assert third.applied_commits == fourth.applied_commits == ()
        assert len(prevention.propose_calls) == 3
        assert sequence.count("publish") == 1
        assert not state.pending_event_revisions(mode=GuardianMode.PROPOSE_PREVENTION)
