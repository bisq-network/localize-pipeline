"""A rejected machine-report lineage must not starve an independent open PR."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from localize.guardian.models import GuardianMode, TrustedActor
from localize.guardian.state import GuardianState
from tests.unit.test_guardian_controller import (
    COMMIT_SHA,
    FakeCodexDriver,
    FakeHistoricalSnapshotProvider,
    FakeHistoricalCheckoutFactory,
    FakeCurrentBaseProvider,
    _config,
    _controller,
    _feedback,
    _historical_policy,
    _pull,
    _snapshot,
    runtime,
)

controller_runtime = runtime


@pytest.mark.parametrize("failure", ["bound", "cycle"])
def test_bad_lineage_fails_only_its_pr_without_partial_authority(
    tmp_path,
    monkeypatch,
    controller_runtime,
    failure,
):
    base, head, checkout, provider, broker, sequence = controller_runtime
    broken = _snapshot(
        pull=_pull(
            number=13, pull_id=501, html_url="https://github.com/acme/widgets/pull/13"
        ),
        feedback=(_feedback(pull_number=13, source_id="45"),),
    )
    provider.snapshots = (broken, _snapshot())
    driver = FakeCodexDriver()
    history = FakeHistoricalSnapshotProvider((), sequence=sequence)
    policy = replace(
        _historical_policy(),
        quality_report_actor=TrustedActor("translation-service", 8, "Bot"),
    )
    calls = []
    with GuardianState(tmp_path / "state.sqlite3") as state:
        actual_lookup = state.replied_publication_for_head

        def lineage(**kwargs):
            if kwargs["pr_number"] != 13:
                return actual_lookup(**kwargs)
            calls.append(kwargs["head_sha"])
            return SimpleNamespace(
                original_head_sha=(
                    kwargs["head_sha"] if failure == "cycle" else f"{len(calls):040x}"
                ),
            )

        monkeypatch.setattr(state, "replied_publication_for_head", lineage)
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            broker=broker,
            driver=driver,
            historical_snapshot_provider=history,
            historical_checkout_factory=FakeHistoricalCheckoutFactory(
                base, head, tmp_path
            ),
            current_base_provider=FakeCurrentBaseProvider(),
        ).poll_once()

        assert outcome.pull_requests_seen == 2
        assert outcome.applied_commits == (COMMIT_SHA,)
        assert len(driver.calls) == 1
        assert len(calls) == (100 if failure == "bound" else 1)
        assert sequence.count("publish") == 1
        assert outcome.failures == ("QualityReportLineageError",)
        assert not state.latest_event_revisions(repository="acme/widgets", pr_number=13)
        assert outcome.historical_repositories_polled == 0
        failed = state.latest_health("guardian-snapshot")
        assert failed.status == "failed"
        assert failed.details == {
            "repository": "acme/widgets",
            "pr_number": 13,
            "failure_type": "QualityReportLineageError",
        }
        assert state.latest_health("guardian").status == "failed"
