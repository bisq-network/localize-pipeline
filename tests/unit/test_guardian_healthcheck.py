"""Healthchecks must reflect completed polls, including failed and skipped work."""

import ast
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

import pytest

from localize.guardian import healthcheck
from localize.guardian.state import GuardianState

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE = PROJECT_ROOT / "localize" / "guardian" / "healthcheck.py"


def _record(path, status, component="guardian"):
    with GuardianState(path) as ledger:
        ledger.record_health(component=component, status=status, message="test")


@pytest.fixture
def state(tmp_path):
    """A real Guardian ledger whose latest whole poll succeeded."""
    path = tmp_path / "state.sqlite3"
    _record(path, "ok")
    return path


@pytest.mark.parametrize(
    "status,exit_code,expected",
    [
        ("ok", 0, "success"),
        ("failed", 1, "fail"),
        ("failed", 0, "fail"),
        ("ok", 1, "fail"),
        (None, 0, None),
        (None, 1, "fail"),
    ],
)
def test_poll_outcome_requires_fresh_audit_success(
    state, monkeypatch, status, exit_code, expected
):
    pings = []

    def run(command, **kwargs):
        assert command == ["guardian", "run"]
        if status:
            _record(state, status)
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr(healthcheck.subprocess, "run", run)
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    result = healthcheck.run_monitored(
        ["guardian", "run"], state, "https://hc-ping.com/test"
    )
    assert pings == ([] if expected is None else [expected])
    assert result == (1 if expected == "fail" else 0)


@pytest.mark.parametrize("ledger_existed", [True, False])
@pytest.mark.parametrize("exit_code,expected", [(0, None), (1, "fail")])
def test_empty_ledger_before_first_poll(
    tmp_path, monkeypatch, ledger_existed, exit_code, expected
):
    """A not-due wake on a fresh install is a skip, not a failure."""
    path = tmp_path / "state.sqlite3"
    if ledger_existed:
        GuardianState(path).close()

    def run(command, **kwargs):
        GuardianState(path).close()  # Guardian opens its ledger, then skips.
        return subprocess.CompletedProcess(command, exit_code)

    pings = []
    monkeypatch.setattr(healthcheck.subprocess, "run", run)
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    result = healthcheck.run_monitored(["guardian"], path, "https://hc-ping.com/test")
    assert pings == ([] if expected is None else [expected])
    assert result == exit_code


def test_first_poll_on_fresh_install_pings_success(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        lambda command, **k: _record(path, "ok") or subprocess.CompletedProcess(command, 0),
    )
    pings = []
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    assert healthcheck.run_monitored(["guardian"], path, "https://hc-ping.com/test") == 0
    assert pings == ["success"]


def test_missing_ledger_after_command_is_a_failure(tmp_path, monkeypatch):
    """A wrong --state path must not hide every poll as a skipped wake."""
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        lambda command, **k: subprocess.CompletedProcess(command, 0),
    )
    pings = []
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    assert (
        healthcheck.run_monitored(
            ["guardian"], tmp_path / "missing.sqlite3", "https://hc-ping.com/test"
        )
        == 1
    )
    assert pings == ["fail"]


@pytest.mark.parametrize(
    "component", ["guardian-poll-attempt", "guardian-responsive-schedule"]
)
def test_attempt_records_alone_do_not_renew_heartbeat(state, monkeypatch, component):
    def run(command, **kwargs):
        _record(state, "attempted", component=component)
        return subprocess.CompletedProcess(command, 0)

    pings = []
    monkeypatch.setattr(healthcheck.subprocess, "run", run)
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    assert (
        healthcheck.run_monitored(["guardian"], state, "https://hc-ping.com/test") == 0
    )
    assert pings == []


def test_broken_audit_does_not_prevent_guardian_running(tmp_path, monkeypatch):
    path = tmp_path / "broken.sqlite3"
    path.write_text("not a database")
    called = []
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        lambda *a, **k: called.append(True) or subprocess.CompletedProcess(a, 0),
    )
    pings = []
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    assert (
        healthcheck.run_monitored(["guardian"], path, "https://hc-ping.com/test") == 1
    )
    assert called == [True]
    assert pings == ["fail"]


def test_ping_error_is_redacted_and_preserves_poll_failure(state, monkeypatch, capsys):
    monkeypatch.setattr(
        healthcheck.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 7)
    )

    def fail(*args):
        raise OSError("https://hc-ping.com/private-token")

    monkeypatch.setattr(healthcheck, "ping", fail)
    assert (
        healthcheck.run_monitored(
            ["guardian"], state, "https://hc-ping.com/private-token"
        )
        == 7
    )
    captured = capsys.readouterr()
    assert "private-token" not in captured.out + captured.err


@pytest.mark.parametrize(
    "response", [b"OK (not found)", b"OK (rate limited)", b"unexpected"]
)
def test_http_200_without_acceptance_is_not_success(monkeypatch, response):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return response

    class Opener:
        def open(self, request, timeout):
            assert request.data == b""
            assert timeout == 10
            return Response()

    monkeypatch.setattr(healthcheck, "build_opener", lambda *args: Opener())
    with pytest.raises(OSError, match="accepted"):
        healthcheck.ping("https://hc-ping.com/test", "success")


@pytest.mark.parametrize(
    "url",
    [
        "http://hc-ping.com/test",
        "https://user:pass@hc-ping.com/test",
        "https://hc-ping.com/test?x=1",
        "https://hc-ping.com/test\nextra",
    ],
)
def test_reject_invalid_url(tmp_path, url):
    path = tmp_path / "url"
    path.write_text(url)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        healthcheck.read_url(path)


def test_reject_public_url_file(tmp_path):
    path = tmp_path / "url"
    path.write_text("https://hc-ping.com/test\n")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        healthcheck.read_url(path)


def test_fifo_url_file_does_not_block_the_poll(tmp_path):
    """An invalid monitoring file must not stop Guardian from running."""
    url_file = tmp_path / "url"
    os.mkfifo(url_file, 0o600)
    marker = tmp_path / "ran"
    result = subprocess.run(
        [
            sys.executable, "-I", str(MODULE),
            "--url-file", str(url_file),
            "--state", str(tmp_path / "state.sqlite3"),
            "--", sys.executable, "-c",
            "import sys; open(sys.argv[1], 'w').close()", str(marker),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert marker.exists()
    assert "unavailable or invalid" in result.stderr
    assert result.returncode == 1


def test_real_child_completion_is_observed(state, monkeypatch):
    """A separate process commits to the WAL ledger; the read-only audit sees it."""
    pings = []
    monkeypatch.setattr(healthcheck, "ping", lambda url, outcome: pings.append(outcome))
    script = (
        "import sys; sys.path.insert(0, sys.argv[2]); "
        "from localize.guardian.state import GuardianState; "
        "ledger = GuardianState(sys.argv[1]); "
        "ledger.record_health(component='guardian', status='ok', message='child'); "
        "ledger.close()"
    )
    assert (
        healthcheck.run_monitored(
            [sys.executable, "-c", script, str(state), str(PROJECT_ROOT)],
            state,
            "https://hc-ping.com/test",
        )
        == 0
    )
    assert pings == ["success"]


def test_invalid_url_file_still_runs_poll(state, tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        lambda *a, **k: called.append(a[0]) or subprocess.CompletedProcess(a, 0),
    )
    assert (
        healthcheck.main(
            [
                "--url-file",
                str(tmp_path / "missing"),
                "--state",
                str(state),
                "--",
                "guardian",
                "run",
            ]
        )
        == 1
    )
    assert called == [["guardian", "run"]]
    assert "unavailable or invalid" in capsys.readouterr().err


def test_redirects_are_not_followed():
    assert (
        healthcheck._NoRedirect().redirect_request(
            None, None, 302, None, None, "https://other.test"
        )
        is None
    )


@pytest.mark.parametrize("outcome,suffix", [("success", ""), ("fail", "/fail")])
def test_accepted_ping_sends_empty_body(monkeypatch, outcome, suffix):
    class Response:
        status = 200

        def read(self, size):
            return b"OK"

    class Opener:
        @contextmanager
        def open(self, request, timeout):
            assert request.full_url == "https://hc-ping.com/test" + suffix
            assert request.data == b""
            assert request.get_method() == "POST"
            yield Response()

    monkeypatch.setattr(healthcheck, "build_opener", lambda *args: Opener())
    healthcheck.ping("https://hc-ping.com/test", outcome)


def test_module_uses_only_the_standard_library():
    """Operators may install the file beside a scheduler, outside the package."""
    imported = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module.split(".")[0])
    assert imported - {"__future__"} <= sys.stdlib_module_names


def test_module_runs_as_a_standalone_file(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(MODULE), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--url-file" in result.stdout
