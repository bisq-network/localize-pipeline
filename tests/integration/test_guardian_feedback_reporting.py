"""Real controller, files, SQLite, and replay; only network/model are faked."""

from dataclasses import replace

from localize.guardian.models import GuardianMode
from localize.guardian.state import GuardianState
from tests.unit.test_guardian_controller import (
    COMMIT_SHA,
    TARGET_PATH,
    FakeCodexDriver,
    TwoReplacementCodexDriver,
    _config,
    _controller,
    _feedback,
    _pull,
    _snapshot,
)
from tests.unit.test_guardian_controller import runtime as _runtime_fixture

runtime = _runtime_fixture


class DecisionDriver(FakeCodexDriver):
    @staticmethod
    def decision(result):
        return replace(
            result,
            feedback=(
                replace(
                    result.feedback[0],
                    verdict="needs_human",
                    replacements=(),
                    report_reason="glossary_conflict",
                    decision_required=True,
                    rationale="Private explanation /Users/operator/secrets password=do-not-publish",
                ),
            ),
        )

    def run(self, task, **kwargs):
        observer = kwargs.pop("success_observer", None)

        def completed(attempt, usage, result):
            if observer:
                observer(attempt, usage, self.decision(result))

        return self.decision(super().run(task, success_observer=completed, **kwargs))


def test_human_explanation_and_summary_are_durable_without_repeated_model_calls(
    tmp_path, runtime
):
    _, _, checkout, provider, broker, _ = runtime
    driver = DecisionDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        first = controller.poll_once()
        assert first.failures == ()
        assert first.applied_commits == ()
        assert len(broker.feedback_reports) == 1
        body = next(iter(broker.feedback_reports.values())).body
        assert "configured glossary" in body and "maintainer decision" in body
        assert "/Users/" not in body and "password" not in body
        assert "maintainer decision still required" in broker.feedback_summary.body
        second = controller.poll_once()
        assert second.failures == () and second.runs_started == 0
        assert len(driver.calls) == 1 and len(broker.feedback_reports) == 1


def test_lost_ack_retries_reporting_not_assessment(tmp_path, runtime):
    _, _, checkout, provider, broker, _ = runtime
    driver = DecisionDriver()
    broker.report_error = RuntimeError("response lost after comment creation")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        first = controller.poll_once()
        assert first.failures == ("RuntimeError",)
        assert state.latest_health("feedback-reporting").status == "failed"
        assert len(broker.feedback_reports) == 1
        broker.report_error = None
        assert controller.poll_once().failures == ()
        assert len(driver.calls) == 1 and len(broker.feedback_reports) == 1
        assert state.latest_health("feedback-reporting").status == "ok"


def test_new_head_cannot_make_model_silently_overrule_held_decision(tmp_path, runtime):
    _, _, checkout, provider, broker, _ = runtime
    with GuardianState(tmp_path / "state.sqlite3") as state:
        common = dict(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            broker=broker,
        )
        assert _controller(**common, driver=DecisionDriver()).poll_once().failures == ()
        provider.snapshots = (_snapshot(pull=_pull(head_sha="d" * 40)),)
        second = _controller(**common, driver=FakeCodexDriver()).poll_once()
        assert second.failures == ()
        assert second.applied_commits == () and second.prepared_value_edits == 0
        assert len(broker.feedback_reports) == 1


def test_observation_does_not_publish_explanations(tmp_path, runtime):
    _, _, checkout, provider, broker, _ = runtime
    with GuardianState(tmp_path / "state.sqlite3") as state:
        result = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=DecisionDriver(),
            broker=broker,
        ).poll_once()
        assert result.failures == ()
        assert broker.feedback_reports == {} and broker.feedback_summary is None


def test_published_correction_report_recovers_on_exact_successor_without_model_call(
    tmp_path, runtime
):
    _, _, checkout, provider, broker, _ = runtime
    driver = FakeCodexDriver()
    broker.report_error = RuntimeError("report acknowledgement lost")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        first = controller.poll_once()
        assert first.applied_commits == (COMMIT_SHA,)
        assert state.feedback_reporting_counts()[0] == 1
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)
        broker.report_error = None
        second = controller.poll_once()
        assert second.failures == () and second.applied_commits == ()
        assert len(driver.calls) == 1 and len(broker.feedback_reports) == 1
        assert state.feedback_reporting_counts()[0] == 0
        assert broker.feedback_summary is not None


def test_withdrawn_feedback_clears_summary_without_claiming_policy_approval(
    tmp_path, runtime
):
    _, _, checkout, provider, broker, _ = runtime
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=DecisionDriver(),
            broker=broker,
        )
        assert controller.poll_once().failures == ()
        provider.snapshots = (_snapshot(feedback=()),)
        assert controller.poll_once().failures == ()
        assert "No current authorized feedback reports" in broker.feedback_summary.body
        assert "does not approve" in broker.feedback_summary.body
        assert state.feedback_reporting_counts()[1] == 1


def test_summary_can_return_to_previously_posted_body(tmp_path, runtime):
    _, _, checkout, provider, broker, _ = runtime
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=DecisionDriver(),
            broker=broker,
        )
        assert controller.poll_once().failures == ()
        first = broker.feedback_summary.body
        provider.snapshots = (_snapshot(feedback=(_feedback(source_id="45"),)),)
        assert controller.poll_once().failures == ()
        assert broker.feedback_summary.body != first
        provider.snapshots = (_snapshot(),)
        assert controller.poll_once().failures == ()
        assert broker.feedback_summary.body == first


def test_report_retry_leaves_active_queue_when_pull_leaves_open_intake(
    tmp_path, runtime
):
    _, _, checkout, provider, broker, _ = runtime
    driver = DecisionDriver()
    broker.report_error = RuntimeError("response lost")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        assert controller.poll_once().failures == ("RuntimeError",)
        assert state.feedback_reporting_counts() == (1, 1)
        provider.snapshots = ()
        broker.report_error = None
        assert controller.poll_once().failures == ()
        assert state.feedback_reporting_counts() == (0, 1)
        assert len(driver.calls) == 1
        # A reopened eligible pull can reconcile the same exact comment.
        provider.snapshots = (_snapshot(),)
        assert controller.poll_once().failures == ()
        assert len(broker.feedback_reports) == 1 and len(driver.calls) == 1


def test_failed_processing_has_safe_deferred_report_intent(tmp_path, runtime):
    _, _, checkout, provider, broker, _ = runtime
    driver = DecisionDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        config = _config(GuardianMode.APPLY_OWNED_TRANSLATIONS)
        config = replace(
            config, limits=replace(config.limits, max_model_calls_per_day=1)
        )
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=config,
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        assert controller.poll_once().failures == ()
        provider.snapshots = (_snapshot(feedback=(_feedback(source_id="45"),)),)
        controller.poll_once()
        # Next intake reports the durable failure without an extra model call.
        controller.poll_once()
        assert len(driver.calls) == 1
        assert any(
            "Processing did not complete" in reply.body
            and "Deferred work" in reply.body
            for reply in broker.feedback_reports.values()
        )


def test_failed_reassessment_cannot_hide_existing_glossary_decision(tmp_path, runtime):
    _, _, checkout, provider, broker, _ = runtime
    with GuardianState(tmp_path / "state.sqlite3") as state:
        common = dict(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            broker=broker,
        )
        assert _controller(**common, driver=DecisionDriver()).poll_once().failures == ()
        provider.snapshots = (_snapshot(pull=_pull(head_sha="d" * 40)),)
        failing = _controller(
            **common, driver=FakeCodexDriver(error=RuntimeError("private failure"))
        )
        assert failing.poll_once().failures == ("RuntimeError",)
        assert state.feedback_reporting_counts()[1] == 1
        assert "maintainer decision still required" in broker.feedback_summary.body
        failing.poll_once()
        assert "maintainer decision still required" in broker.feedback_summary.body
        assert state.feedback_reporting_counts()[1] == 1


def test_partial_glossary_alternative_explicitly_holds_remaining_edits(
    tmp_path, runtime
):
    base, head, checkout, provider, broker, _ = runtime
    for root in (base, head):
        source = root / "l10n/messages_en.properties"
        source.write_text(source.read_text() + "alpha=Alpha\n")
        target = root / TARGET_PATH
        target.write_text(target.read_text() + "alpha=Старый альфа\n")

    class AlternativeDriver(TwoReplacementCodexDriver):
        @staticmethod
        def _with_alpha(result):
            result = TwoReplacementCodexDriver._with_alpha(result)
            return replace(
                result,
                feedback=(
                    replace(
                        result.feedback[0],
                        report_reason="alternative_glossary",
                        decision_required=True,
                    ),
                ),
            )

    config = _config(GuardianMode.APPLY_OWNED_TRANSLATIONS)
    config = replace(config, limits=replace(config.limits, max_value_edits_per_run=1))
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=config,
            checkout=checkout,
            provider=provider,
            driver=AlternativeDriver(),
            broker=broker,
        )
        first = controller.poll_once()
        assert first.applied_commits == (COMMIT_SHA,)
        assert first.deferred_value_edits == 1
        assert any(
            "1 remaining value edits are human-held" in reply.body
            for reply in broker.feedback_reports.values()
        )
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)
        second = controller.poll_once()
        assert second.failures == () and second.applied_commits == ()
        assert state.feedback_reporting_counts()[1] == 1
        assert "maintainer decision still required" in broker.feedback_summary.body
