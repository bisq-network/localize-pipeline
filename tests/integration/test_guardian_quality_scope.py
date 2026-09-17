"""Rebuilt evidence retains only the remaining feedback's path authority."""

from dataclasses import replace
import json

import pytest

from localize.guardian.github import ChangedFile
from localize.guardian.models import GuardianMode, TrustedActor
from localize.guardian.codex import GuardianFeedbackDecision
from localize.guardian.quality_reports import parse_report, render_report
from localize.guardian.state import GuardianState
from tests.integration.test_guardian_quality_intake import EchoDriver, SOURCE, machine_snapshot
from tests.unit.test_guardian_controller import (
    COMMIT_SHA, TARGET_PATH, SECOND_TARGET_PATH, _add_second_localization_target,
    _config, _controller, _feedback, _policy, runtime,
)

controller_runtime = runtime


@pytest.mark.parametrize("valid_kind", ["machine", "reviewer"])
def test_invalid_machine_only_path_is_removed_without_narrowing_reviewer_authority(
    tmp_path, controller_runtime, valid_kind,
):
    base, head, checkout, provider, broker, _sequence = controller_runtime
    for root in (base, head):
        _add_second_localization_target(root)
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n", encoding="utf-8")
    snapshot = machine_snapshot()
    report = parse_report(snapshot.feedback[0].body)
    report["path"] = SECOND_TARGET_PATH
    report["findings"][0]["key"] = "failure"
    report["findings"][0]["source_sha256"] = "f" * 64
    invalid = replace(snapshot.feedback[0], source_id="42", body=render_report(report))
    valid = snapshot.feedback[0] if valid_kind == "machine" else _feedback(source_id="45")
    provider.snapshots = (replace(
        snapshot, feedback=(invalid, valid), changed_files=(*snapshot.changed_files, ChangedFile(
            path=SECOND_TARGET_PATH, status="modified", sha="e" * 40,
            patch="@@ -1 +1 @@\n-old\n+invalid-only-path-context",
        )),
    ),)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))

    class InspectDriver(EchoDriver):
        def run(self, task, **kwargs):
            manifest = json.loads((task.evidence_dir / "manifest.json").read_text())
            localization = json.loads((task.evidence_dir / "localization.json").read_text())
            paths = {item["path"] for item in localization}
            expected = {TARGET_PATH} if valid_kind == "machine" else {TARGET_PATH, SECOND_TARGET_PATH}
            assert set(manifest["files"]) == paths == expected
            assert ("invalid-only-path-context" in (task.evidence_dir / "changes.diff").read_text()) == (valid_kind == "reviewer")
            assert not (task.evidence_dir.parent / "bundle").exists()
            observer = kwargs.pop("success_observer", None)
            def add_private_overlap(result):
                if valid_kind != "reviewer":
                    return result
                extra_ids = set(manifest["feedback_ids"]) - {item.feedback_id for item in result.feedback}
                assert all(identifier.startswith("quality_finding:") for identifier in extra_ids)
                return replace(result, feedback=(*result.feedback, *(GuardianFeedbackDecision(
                    feedback_id=identifier, verdict="reject", confidence=0.99,
                    rationale="The correction is assigned to the overlapping reviewer item.",
                    replacements=(),
                ) for identifier in sorted(extra_ids))))
            def success(attempt, usage, result):
                if observer is not None:
                    observer(attempt, usage, add_private_overlap(result))
            return add_private_overlap(super().run(task, success_observer=success, **kwargs))

    driver = InspectDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path, state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout, provider=provider, broker=broker, driver=driver,
        ).poll_once()
        assert outcome.failures == ()
        assert outcome.applied_commits == (COMMIT_SHA,)
        assert len(driver.calls) == 1
