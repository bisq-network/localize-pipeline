"""Quality intake comes from immutable checkouts, not public payloads."""

from dataclasses import replace
from contextlib import contextmanager
import shutil

import pytest

from localize.guardian.models import GuardianMode, TrustedActor
from localize.guardian.state import GuardianState
from tests.integration.test_guardian_quality_intake import EchoDriver, SOURCE, machine_snapshot
from localize.guardian.prevention_runtime import PreventionBatchOutcome
from tests.unit.test_guardian_controller import (
    TARGET_PATH, COMMIT_SHA, HEAD_SHA, _config, _controller, _policy, _snapshot, runtime,
    FakePreventionRunner, _prevention_policy,
)

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
