"""Private diagnostics survive real adapter/controller boundaries without raw output."""

import json
import subprocess

import httpx
import pytest

from localize.guardian.controller import _safe_failure_name
from localize.guardian.github import GitHubAPIError, _GitHubHTTP
from localize.guardian.state import GuardianState
from localize.guardian.workspace import WorkspaceError, _GitRunner


@pytest.mark.parametrize(
    "operation,stderr,stage,reason",
    [
        ("commit", "error: gpg failed to sign the data", "sign", "signing_failed"),
        ("push", "remote: Permission denied", "push", "permission_denied"),
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
