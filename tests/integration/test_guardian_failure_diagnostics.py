"""Private diagnostics survive real adapter/controller boundaries without raw output."""

import json
import subprocess
from datetime import datetime, timezone

import httpx
import pytest

from localize.guardian.controller import _safe_failure_name
from localize.guardian.github import GitHubAPIError, _GitHubHTTP
from localize.guardian.state import GuardianState
from localize.guardian.workspace import WorkspaceError, _GitRunner
from tests.unit.test_guardian_controller import (
    FakeCodexDriver,
    _config,
    _controller,
    runtime,
)

controller_runtime = runtime


def test_prevention_signing_failure_survives_coordinator_catch(tmp_path, monkeypatch):
    from localize.guardian.diagnostics import failure_audit, git_failure
    from localize.guardian.models import GuardianMode
    from tests.unit.test_guardian_prevention_runtime import (
        OPEN_SOURCE_REVISION_ID, _FakeAuthor, _FakeBroker, _FakeWorkspace,
        _candidate, _coordinator, _current_base, _live_lease,
        _open_source_kwargs, _repository_policy,
    )

    def fail_signing(*args, **kwargs):
        raise git_failure(
            WorkspaceError("private signing details"), operation="commit",
            returncode=128, output="gpg failed to sign private-secret",
        )

    monkeypatch.setattr(_FakeWorkspace, "commit_prevention_changes", fail_signing)
    now = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
    with GuardianState(tmp_path / "state.sqlite3") as state, failure_audit(state):
        run_id = state.start_run(repository="acme/translations", locale="ru",
                                 mode=GuardianMode.PROPOSE_PREVENTION, started_at=now)
        coordinator = _coordinator(state=state, tmp_path=tmp_path,
                                   broker=_FakeBroker(), author=_FakeAuthor())
        outcome = coordinator.propose(
            policy=_repository_policy(), recurrence_candidates=(_candidate(),),
            evidence_revision_ids={"review_comment:42": OPEN_SOURCE_REVISION_ID},
            run_id=run_id, observed_at=now, require_live_lease=_live_lease,
            require_current_base_unchanged=_current_base, **_open_source_kwargs(),
        )
        assert outcome.failures == ("WorkspaceError",)
        details = state.latest_health("guardian-failure").details
        assert details["stage"] == "sign"
        assert details["reason"] == "signing_failed"
        assert details["run_id"] == run_id
        assert "private" not in json.dumps(details)


@pytest.mark.parametrize(
    "operation,stderr,stage,reason",
    [
        ("commit", "error: gpg failed to sign the data", "sign", "signing_failed"),
        ("push", "remote: Permission denied", "push", "permission_denied"),
        (
            "push",
            "refusing to allow an OAuth App to create or update workflow "
            "`.github/workflows/build.yml` without `workflow` scope",
            "push",
            "workflow_scope_required",
        ),
        ("ls-remote", "fatal: Could not resolve host", "remote-read", "dns_failed"),
    ],
)
def test_git_failure_reaches_private_audit_without_output_leak(
    tmp_path,
    operation,
    stderr,
    stage,
    reason,
):
    from localize.guardian.diagnostics import failure_audit

    secret = "private-credential-do-not-record"
    raw = f"{stderr}\n{secret} /Users/private/operator/key https://user:{secret}@host/\x1b[31m"

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, raw, raw)

    runner = _GitRunner(tmp_path, {}, process)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        with failure_audit(state):
            try:
                runner.run((operation,))
            except WorkspaceError as error:
                assert (
                    _safe_failure_name(
                        error,
                        repository="acme/widgets",
                        run_id="run-123",
                        pull_numbers=[7],
                    )
                    == "WorkspaceError"
                )
        record = state.latest_health("guardian-failure")
        assert record is not None
        details = record.details
        assert details["stage"] == stage
        assert details["operation"] == operation
        assert details["exit_code"] == 128
        assert details["reason"] == reason
        assert details["repository"] == "acme/widgets"
        assert details["pull_numbers"] == [7]
        assert details["run_id"] == "run-123"
        assert details["locations"]
        assert secret not in json.dumps(details)
        assert "/Users/" not in json.dumps(details)
        assert "https://" not in json.dumps(details)


def test_pr_creation_http_failure_retains_status_but_not_response(tmp_path):
    from localize.guardian.diagnostics import failure_audit

    def transport(request):
        return httpx.Response(422, json={"message": "secret /Users/private"})

    with GuardianState(tmp_path / "state.sqlite3") as state:
        with (
            failure_audit(state),
            httpx.Client(transport=httpx.MockTransport(transport)) as client,
        ):
            try:
                _GitHubHTTP(client).request_json(
                    "POST", "https://api.github.com/repos/acme/widgets/pulls"
                )
            except GitHubAPIError as error:
                _safe_failure_name(error, repository="acme/widgets", run_id="run-456")
        details = state.latest_health("guardian-failure").details
        assert details["stage"] == "create-pr"
        assert details["http_status"] == 422
        assert details["reason"] == "http_error"
        assert "secret" not in json.dumps(details)
        assert "/Users/" not in json.dumps(details)


def test_diagnostic_scope_restores_after_exception_and_bounds_records(tmp_path):
    from localize.guardian.diagnostics import failure_audit

    with GuardianState(tmp_path / "state.sqlite3") as state:
        with pytest.raises(RuntimeError), failure_audit(state):
            for _ in range(100):
                _safe_failure_name(RuntimeError("arbitrary secrets must not survive"))
            raise RuntimeError("another secret")
        rows = state._connection.execute(
            "SELECT details_json FROM health WHERE component='guardian-failure'"
        ).fetchall()
        assert 1 <= len(rows) <= 32
        _safe_failure_name(RuntimeError("outside scope"))
        assert (
            len(rows)
            == state._connection.execute(
                "SELECT COUNT(*) FROM health WHERE component='guardian-failure'"
            ).fetchone()[0]
        )
        assert all("secret" not in row[0] for row in rows)


def test_same_exception_is_not_recorded_again_at_outer_boundary(tmp_path):
    from localize.guardian.diagnostics import failure_audit, record_failure

    with GuardianState(tmp_path / "state.sqlite3") as state, failure_audit(state):
        error = RuntimeError("private detail")
        record_failure(
            error, run_id="run-1", repository="acme/widgets", pull_numbers=[7]
        )
        _safe_failure_name(error)
        record = state.latest_health("guardian-failure")
        assert record.details["run_id"] == "run-1"
        assert (
            state._connection.execute(
                "SELECT COUNT(*) FROM health WHERE component='guardian-failure'"
            ).fetchone()[0]
            == 1
        )


def test_open_assessment_failure_keeps_run_and_pr_identity(
    tmp_path, controller_runtime
):
    from localize.guardian.codex import CodexOutputError
    from localize.guardian.diagnostics import failure_audit
    from localize.guardian.models import GuardianMode

    _base, _head, checkout, provider, broker, _sequence = controller_runtime

    def fail_conversion(*_args, **_kwargs):
        raise CodexOutputError("private replacement data")

    with GuardianState(tmp_path / "state.sqlite3") as state, failure_audit(state):
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=FakeCodexDriver(),
            broker=broker,
        )
        controller.assessment_converter = fail_conversion
        outcome = controller.poll_once()
        assert outcome.runs_failed == 1
        diagnostic = state.latest_health("guardian-failure")
        assert diagnostic.details["repository"] == "acme/widgets"
        assert diagnostic.details["pull_numbers"]
        assert state.get_run(diagnostic.details["run_id"]).status == "failed"
        assert "private replacement" not in json.dumps(diagnostic.details)
