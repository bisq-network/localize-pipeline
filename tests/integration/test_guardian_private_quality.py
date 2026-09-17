"""Quality intake comes from immutable checkouts, not public payloads."""

from dataclasses import replace
from contextlib import contextmanager
import shutil
import json

import pytest

from localize.guardian.models import GuardianMode, TrustedActor
from localize.guardian.state import GuardianState
from tests.integration.test_guardian_quality_intake import EchoDriver, SOURCE, machine_snapshot
from tests.integration.test_guardian_quality_intake import MultiEchoDriver
from localize.guardian.quality_reports import build_report, finding, render_report
from localize.guardian.codex import GuardianRecurrenceCandidate
from localize.guardian.prevention_runtime import PreventionBatchOutcome
from tests.unit.test_guardian_controller import (
    TARGET_PATH, COMMIT_SHA, HEAD_SHA, _config, _controller, _policy, _snapshot, runtime,
    FakePreventionRunner, _prevention_policy, _feedback,
)
from localize.guardian.github import FeedbackKind
from localize.guardian.private_quality import lineage_finding_identity

controller_runtime = runtime


def immutable_head_checkout(checkout, head, tmp_path):
    original = tmp_path / "immutable-original-head"
    shutil.copytree(head, original)
    @contextmanager
    def checked(revision):
        old = checkout.head_tree
        checkout.head_tree = original if revision.sha == HEAD_SHA else head
        try:
            with checkout(revision) as workspace:
                yield workspace
        finally:
            checkout.head_tree = old
    return checked


def test_private_finding_repairs_without_any_public_report(tmp_path, controller_runtime):
    _base, head, checkout, provider, broker, _sequence = controller_runtime
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n", encoding="utf-8")
    provider.snapshots = (replace(_snapshot(), feedback=()),)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))
    driver = EchoDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout, provider=provider, broker=broker, driver=driver,
        )
        first = controller.poll_once()
        assert first.failures == ()
        assert first.applied_commits == (COMMIT_SHA,)
        revisions = state.latest_event_revisions()
        assert len(revisions) == 1
        assert revisions[0].kind == "quality_finding"
        assert revisions[0].event_id.startswith("internal:quality:")
        assert len(driver.calls) == 1
        assert "source_sha256" not in broker.feedback_summary.body
        assert "/commit/" in broker.feedback_summary.body
        assert f"/commit/{COMMIT_SHA}" in broker.feedback_summary.body
        provider.snapshots = (replace(provider.snapshots[0], pull_request=replace(
            provider.snapshots[0].pull_request, head_sha=COMMIT_SHA,
        )),)
        assert controller.poll_once().failures == ()
        assert len(driver.calls) == 1


def test_private_scan_is_opt_in_and_does_not_assess_unchanged_values(tmp_path, controller_runtime):
    base, head, checkout, provider, broker, _sequence = controller_runtime
    for root in (base, head):
        (root / TARGET_PATH).write_text("greeting=" + SOURCE + "\n", encoding="utf-8")
    provider.snapshots = (replace(_snapshot(), feedback=()),)
    driver = EchoDriver()
    for enabled in (False, True):
        policy = replace(_policy(), quality_report_actor=(TrustedActor("producer", 8, "User") if enabled else None))
        with GuardianState(tmp_path / f"state-{enabled}.sqlite3") as state:
            controller = _controller(
                tmp_path=tmp_path, state=state,
                config=_config(GuardianMode.OBSERVE, policies=(policy,)),
                checkout=checkout, provider=provider, broker=broker, driver=driver,
            )
            outcome = controller.poll_once()
            assert outcome.failures == ()
            assert not state.latest_event_revisions()
    assert not driver.calls


@pytest.mark.parametrize("legacy_first", [False, True])
def test_private_prevention_survives_own_fix_and_legacy_cleanup(
    tmp_path, controller_runtime, legacy_first,
):
    _base, head, checkout, provider, broker, _sequence = controller_runtime
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n")
    checked = immutable_head_checkout(checkout, head, tmp_path)
    snapshot = machine_snapshot() if legacy_first else replace(_snapshot(), feedback=())
    provider.snapshots = (snapshot,)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"), prevention=_prevention_policy())
    config = _config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,))
    driver = EchoDriver()
    driver.recurrence = True
    prevention = FakePreventionRunner(result=PreventionBatchOutcome(failures=("WorkspaceError",)))
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(tmp_path=tmp_path, state=state, config=config,
                                 checkout=checked, provider=provider, broker=broker,
                                 driver=driver, prevention_runner=prevention)
        assert controller.poll_once().applied_commits == (COMMIT_SHA,)
        assert len(prevention.propose_calls) == 1
        (head / TARGET_PATH).write_text("greeting=Отправка в %0 отклонена (%1). %2 %3\n")
        provider.snapshots = (replace(snapshot, feedback=(), pull_request=replace(
            snapshot.pull_request, head_sha=COMMIT_SHA,
        )),)
        driver.prevention_only = True
        prevention.result = PreventionBatchOutcome()
        second = controller.poll_once()
        assert second.failures == ()
        assert not second.applied_commits
        assert len(prevention.propose_calls) == 2
        assert not controller.poll_once().applied_commits
        assert len(prevention.propose_calls) == 2


def test_private_scan_uses_trusted_ignore_and_brand_rules(tmp_path, controller_runtime):
    base, head, checkout, provider, broker, _sequence = controller_runtime
    import yaml
    config_file = base / ".localize/config.yaml"
    config = yaml.safe_load(config_file.read_text())
    config["ignore_key_patterns"] = ["^greeting$"]
    config["brand_technical_glossary"] = ["Bisq"]
    config_file.write_text(yaml.safe_dump(config))
    (base / "l10n/messages_en.properties").write_text("greeting=Hello\nbrand=Bisq\n")
    (base / TARGET_PATH).write_text("greeting=Привет\nbrand=Старый\n")
    (head / TARGET_PATH).write_text("greeting=Hello\nbrand=Bisq\n")
    provider.snapshots = (replace(_snapshot(), feedback=()),)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))
    driver = EchoDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.OBSERVE, policies=(policy,)), checkout=checkout,
            provider=provider, broker=broker, driver=driver).poll_once()
        assert outcome.failures == ()
        assert not state.latest_event_revisions()
        assert not driver.calls


@pytest.mark.parametrize("bound", ["MAX_PRIVATE_EVIDENCE_BYTES", "MAX_PRIVATE_FINDINGS"])
def test_private_scan_fails_closed_before_state_and_model_when_bounded(
    tmp_path, controller_runtime, monkeypatch, bound,
):
    _base, head, checkout, provider, broker, _sequence = controller_runtime
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n")
    provider.snapshots = (replace(_snapshot(), feedback=()),)
    monkeypatch.setattr("localize.guardian.private_quality." + bound, 0)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))
    driver = EchoDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout, provider=provider, broker=broker, driver=driver).poll_once()
        assert outcome.failures
        assert not state.latest_event_revisions()
        assert not driver.calls
        assert not outcome.applied_commits


@pytest.mark.parametrize("race_call", [1, 2])
def test_private_findings_cannot_publish_after_fresh_head_race(
    tmp_path, controller_runtime, monkeypatch, race_call,
):
    _base, head, checkout, provider, broker, _sequence = controller_runtime
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n")
    provider.snapshots = (replace(_snapshot(), feedback=()),)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))
    original = provider.revalidate_open_pull_request
    calls = 0
    def raced(*args, **kwargs):
        nonlocal calls
        calls += 1
        fresh = original(*args, **kwargs)
        return (replace(fresh, pull_request=replace(fresh.pull_request, head_sha="e" * 40))
                if calls >= race_call else fresh)
    monkeypatch.setattr(provider, "revalidate_open_pull_request", raced)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout, provider=provider, broker=broker, driver=EchoDriver()).poll_once()
        assert outcome.failures
        assert not outcome.applied_commits
        assert "publish" not in _sequence


@pytest.mark.parametrize("include_review", [False, True])
def test_combined_private_intake_bound_leaves_no_partial_state(
    tmp_path, controller_runtime, monkeypatch, include_review,
):
    base, head, checkout, provider, broker, _sequence = controller_runtime
    source = "greeting=" + SOURCE + "\nsecond=Hello reader\n"
    (base / "l10n/messages_en.properties").write_text(source)
    (head / TARGET_PATH).write_text(source)
    provider.snapshots = (replace(_snapshot(), feedback=(_feedback(),) if include_review else ()),)
    monkeypatch.setattr("localize.guardian.controller._MAX_CURRENT_FEEDBACK_PER_PULL", 2 if include_review else 1)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))
    driver = EchoDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout, provider=provider, broker=broker, driver=driver).poll_once()
        assert outcome.failures
        assert not state.latest_event_revisions()
        assert not driver.calls
        assert not outcome.applied_commits


def test_middle_head_legacy_report_keeps_pending_prevention_after_two_own_fixes(
    tmp_path, controller_runtime, monkeypatch,
):
    base, head, checkout, provider, broker, _sequence = controller_runtime
    source = "first=Hello reader\ngreeting=Hello reader\n"
    (base / "l10n/messages_en.properties").write_text(source)
    (base / TARGET_PATH).write_text("first=Старый\ngreeting=Старый\n")
    (head / TARGET_PATH).write_text(source)
    checked = immutable_head_checkout(checkout, head, tmp_path)
    snapshot = replace(_snapshot(), feedback=())
    provider.snapshots = (snapshot,)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"), prevention=_prevention_policy())
    config = _config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,))
    config = replace(config, limits=replace(config.limits, max_value_edits_per_run=1, max_model_calls_per_day=50))

    class RecurringDriver(MultiEchoDriver):
        def run(self, task, **kwargs):
            callback = kwargs.pop("success_observer", None)
            entries = json.loads((task.evidence_dir / "localization.json").read_text())[0]["entries"]
            def transform(result):
                decisions = tuple(replace(item, verdict="reject", replacements=(), report_reason="already_addressed")
                                  if entries[item.replacements[0].key]["target"] != "Hello reader" else item
                                  for item in result.feedback)
                return replace(result, feedback=decisions, recurrence_candidates=(GuardianRecurrenceCandidate(
                    scope="pipeline_code", summary="Unexpected repeated English source echoes.",
                    evidence_feedback_ids=tuple(item.feedback_id for item in decisions),
                ),))
            def success(attempt, usage, result):
                if callback:
                    callback(attempt, usage, transform(result))
            return transform(super().run(task, success_observer=success, **kwargs))

    driver = RecurringDriver()
    prevention = FakePreventionRunner(result=PreventionBatchOutcome(failures=("WorkspaceError",)))
    with GuardianState(tmp_path / "state.sqlite3") as state:
        def poll():
            _sequence.clear()
            return _controller(tmp_path=tmp_path, state=state, config=config, checkout=checked,
                               provider=provider, broker=broker, driver=driver,
                               prevention_runner=prevention).poll_once()
        assert poll().applied_commits == (COMMIT_SHA,)
        (head / TARGET_PATH).write_text("first=Привет читатель\ngreeting=Hello reader\n")
        middle_pull = replace(snapshot.pull_request, head_sha=COMMIT_SHA)
        report = build_report(middle_pull, path=TARGET_PATH, locale="ru", findings=[
            finding("greeting", "Hello reader", "Hello reader", "source_echo"),
        ])
        legacy = replace(_feedback(body=render_report(report)), kind=FeedbackKind.ISSUE_COMMENT,
                         path=None, author_id=8, author_type="User")
        provider.snapshots = (replace(snapshot, pull_request=middle_pull, feedback=(legacy,)),)
        monkeypatch.setattr("tests.unit.test_guardian_controller.COMMIT_SHA", "d" * 40)
        middle = poll()
        assert middle.applied_commits == ("d" * 40,), middle
        (head / TARGET_PATH).write_text("first=Привет читатель\ngreeting=Привет читатель\n")
        provider.snapshots = (replace(snapshot, pull_request=replace(middle_pull, head_sha="d" * 40)),)
        prevention.result = PreventionBatchOutcome()
        before = len(prevention.propose_calls)
        final = poll()
        assert final.failures == ()
        assert not final.applied_commits
        assert len(prevention.propose_calls) == before + 1, [
            (r.event_id, r.revision_id, state.latest_feedback_report(r.revision_id))
            for r in state.latest_event_revisions()
        ]
        latest = json.loads(state._connection.execute(
            "SELECT result_json FROM assessment_results ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])
        assert len(latest["feedback"]) == 2
        assert all(item["verdict"] == "reject" for item in latest["feedback"])


def test_lineage_identity_only_ignores_a_durably_authorized_head():
    pull = _snapshot().pull_request
    report = build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("greeting", SOURCE, SOURCE, "source_echo"),
    ])
    heads = {HEAD_SHA, COMMIT_SHA}
    original = lineage_finding_identity(render_report(report), allowed_heads=heads)
    advanced = {**report, "head_sha": COMMIT_SHA}
    assert lineage_finding_identity(render_report(advanced), allowed_heads=heads) == original
    for field, changed in (("base_sha", "e" * 40), ("locale", "fr"), ("repository_id", 99)):
        assert lineage_finding_identity(render_report({**advanced, field: changed}), allowed_heads=heads) != original
    changed_values = {**advanced, "findings": [finding("greeting", SOURCE, "different", "source_echo")]}
    assert lineage_finding_identity(render_report(changed_values), allowed_heads=heads) != original
    with pytest.raises(ValueError, match="outside the durable"):
        lineage_finding_identity(render_report({**report, "head_sha": "f" * 40}), allowed_heads=heads)
