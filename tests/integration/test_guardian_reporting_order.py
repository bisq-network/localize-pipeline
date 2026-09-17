"""Pending status replies must not starve repairs when a review bot responds."""

from dataclasses import replace

from localize.guardian.codex import CodexOutputError
from localize.guardian.models import GuardianMode
from localize.guardian.state import GuardianState
from tests.unit.test_guardian_controller import (
    COMMIT_SHA,
    FakeCodexDriver,
    _config,
    _controller,
    runtime,
)

controller_runtime = runtime


def test_report_triggered_feedback_change_does_not_starve_pending_repair(
    tmp_path, controller_runtime,
):
    """Real authority checks still suppress reporting after the feedback changes."""
    _base, _head, checkout, provider, broker, sequence = controller_runtime
    config = _config(GuardianMode.APPLY_OWNED_TRANSLATIONS)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path, state=state, config=config, checkout=checkout,
            provider=provider, broker=broker,
            driver=FakeCodexDriver(error=CodexOutputError("bad assessment")),
        ).poll_once()
        assert first.runs_failed == 1
        driver = FakeCodexDriver()
        original_report = broker.post_feedback_report

        def reply_triggers_reviewer_update(**kwargs):
            result = original_report(**kwargs)
            sequence.append("status-report")
            snapshot = provider.snapshots[0]
            provider.snapshots = (replace(
                snapshot,
                feedback=(replace(
                    snapshot.feedback[0],
                    body="Reviewer acknowledgement changes the trusted feedback.",
                    updated_at="2026-08-30T12:01:00Z",
                ),),
            ),)
            # The real broker revalidates before further publication too.
            kwargs["before_create"]()
            return result

        broker.post_feedback_report = reply_triggers_reviewer_update
        second = _controller(
            tmp_path=tmp_path, state=state, config=config, checkout=checkout,
            provider=provider, broker=broker, driver=driver,
        ).poll_once()

        assert second.applied_commits == (COMMIT_SHA,)
        assert len(driver.calls) == 1
        assert sequence.index("publish") < sequence.index("status-report")
        assert state.latest_health("feedback-reporting").status == "pending"
        assert broker.feedback_summary is None
