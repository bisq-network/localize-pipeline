"""Production assembly tests for one Localize Guardian poll."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest

from localize.guardian.controller import PollOutcome
from localize.guardian.credentials import CredentialError
from localize.guardian.github import GitHubAuthenticationError
from localize.guardian.models import (
    AllowedHeadRepository,
    CodexAuthMode,
    ExactRepository,
    GuardianConfig,
    GuardianLimits,
    GuardianMode,
    GuardianRuntime,
    GuardianSchedule,
    PreventionPolicy,
    PipelineConfigSnapshot,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.state import GuardianState
from localize.guardian.workspace import ExactRevision
from localize.guardian import runtime


UTC = timezone.utc
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_runtime_authority_for_assembly_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime,
        "_validate_runtime_authority",
        lambda _config, *, scheduled: None,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _policy() -> RepositoryPolicy:
    return RepositoryPolicy(
        base_repo="acme/widgets",
        base_repo_id=101,
        base_branch="main",
        allowed_pr_authors=(TrustedActor("translation-bot", 102, "Bot"),),
        allowed_head_owners=(TrustedActor("contributor", 103, "User"),),
        allowed_head_repositories=(
            AllowedHeadRepository("contributor/widgets", 104),
        ),
        allowed_branch_globs=("localization/**",),
        allowed_path_globs=("l10n/**",),
        pipeline_config_path=".localize/config.yaml",
        source_locale="en",
        trusted_reviewers={"ru": (TrustedActor("reviewer", 105, "User"),)},
        trusted_bots={},
    )


def _prevention_policy() -> PreventionPolicy:
    return PreventionPolicy(
        target_repository=ExactRepository("guardian/pipeline", 201),
        target_base_branch="main",
        push_repository=ExactRepository("guardian/pipeline", 201),
        push_branch_prefix="guardian/prevention-",
        allowed_code_path_globs=("localize/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(("venv/bin/pytest", "tests/unit/test_rule.py", "-q"),),
        sandbox_argv_prefix=("/usr/bin/sandbox-exec", "-f", "/safe.sb"),
        max_changed_files=4,
        max_changed_bytes=16_384,
    )


def _config(mode: GuardianMode = GuardianMode.OBSERVE) -> GuardianConfig:
    policy = _policy()
    if mode is GuardianMode.PROPOSE_PREVENTION:
        policy = replace(policy, prevention=_prevention_policy())
    return GuardianConfig(
        repositories=(policy,),
        mode=mode,
        limits=GuardianLimits(
            run_timeout_seconds=240,
            max_attempts=2,
            daily_cost_limit_usd=5,
            model_call_reservation_usd=5,
        ),
        runtime=GuardianRuntime(
            codex_model="gpt-test",
            codex_reasoning_effort="high",
            codex_auth_mode=CodexAuthMode.API_KEY,
            codex_executable="/opt/bin/codex",
            git_executable="/opt/bin/git",
            signing_program="/opt/bin/gpg",
            github_token_command=("/opt/bin/github-token", "read"),
            codex_api_key_command=("/opt/bin/model-token", "read"),
            signing_key="A" * 40,
        ),
    )


def _write_minimal_config(path: Path) -> None:
    path.write_text(
        """mode: observe
repositories:
  - base_repo: acme/widgets
    base_repo_id: 101
    base_branch: main
    allowed_pr_authors:
      - {login: translation-bot, id: 102, type: Bot}
    allowed_head_owners:
      - {login: contributor, id: 103, type: User}
    allowed_head_repositories:
      - {full_name: contributor/widgets, id: 104}
    allowed_branch_globs: [localization/**]
    allowed_path_globs: [l10n/**]
    pipeline_config_path: .localize/config.yaml
    source_locale: en
    trusted_reviewers:
      ru:
        - {login: reviewer, id: 105, type: User}
    trusted_bots: {}
""",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_subscription_codex_home_must_be_private_file_backed_and_non_symlinked(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"tokens":"redacted-test-value"}', encoding="utf-8")
    auth_file.chmod(0o600)
    config = replace(
        _config(),
        runtime=replace(
            _config().runtime,
            codex_auth_mode=CodexAuthMode.CHATGPT,
            codex_home=str(codex_home),
            codex_api_key_command=(),
        ),
    )

    assert runtime._validate_subscription_codex_home(config) == codex_home

    auth_file.chmod(0o644)
    with pytest.raises(runtime.GuardianRuntimeError, match="authentication"):
        runtime._validate_subscription_codex_home(config)

    auth_file.chmod(0o600)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(codex_home, target_is_directory=True)
    linked_config = replace(
        config,
        runtime=replace(config.runtime, codex_home=str(linked_home)),
    )
    with pytest.raises(runtime.GuardianRuntimeError, match="authentication"):
        runtime._validate_subscription_codex_home(linked_config)


def test_scheduled_executable_authority_is_rechecked_at_runtime(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700)
    commands: list[str] = []
    for name in ("codex", "git", "gpg", "github-token", "model-token"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        commands.append(str(executable))
    config = replace(
        _config(),
        runtime=replace(
            _config().runtime,
            codex_executable=commands[0],
            git_executable=commands[1],
            signing_program=commands[2],
            github_token_command=(commands[3],),
            codex_api_key_command=(commands[4],),
        ),
    )

    runtime._validate_scheduled_executables(config)

    Path(commands[0]).chmod(0o722)
    with pytest.raises(runtime.GuardianRuntimeError, match="executable authority"):
        runtime._validate_scheduled_executables(config)


def test_scheduled_signing_program_is_required_only_for_write_modes(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700)
    commands: dict[str, str] = {}
    for name in ("codex", "git", "github-token", "model-token"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        commands[name] = str(executable)
    config = replace(
        _config(),
        runtime=replace(
            _config().runtime,
            codex_executable=commands["codex"],
            git_executable=commands["git"],
            signing_program="gpg",
            github_token_command=(commands["github-token"],),
            codex_api_key_command=(commands["model-token"],),
        ),
    )

    runtime._validate_scheduled_executables(config)

    write_config = replace(config, mode=GuardianMode.APPLY_OWNED_TRANSLATIONS)
    with pytest.raises(runtime.GuardianRuntimeError, match="executable authority"):
        runtime._validate_scheduled_executables(write_config)


class _Credential:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.calls = 0

    def read(self) -> str:
        self.calls += 1
        return self.secret


def test_authenticated_snapshot_provider_uses_reader_and_passes_all_previous_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _Credential("github-secret")
    previous = (
        SimpleNamespace(source_id="11"),
        SimpleNamespace(source_id="12"),
    )
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    class FakeReader:
        def __init__(self, client: httpx.Client, policy: object) -> None:
            observed["policy"] = policy
            observed["response"] = client.get("/probe").json()

        def collect_open_pull_requests(self, *, previous_feedback: object) -> tuple[str]:
            observed["previous"] = previous_feedback
            return ("snapshot",)

    monkeypatch.setattr(runtime, "GitHubReader", FakeReader)
    provider = runtime.AuthenticatedGitHubSnapshotProvider(
        credential=credential,  # type: ignore[arg-type]
        transport=transport,
    )

    result = provider(_policy(), previous)  # type: ignore[arg-type]

    assert result == ("snapshot",)
    assert credential.calls == 1
    assert observed["authorization"] == "Bearer github-secret"
    assert observed["previous"] is previous
    assert observed["response"] == {"ok": True}
    assert observed["policy"].repository == "acme/widgets"  # type: ignore[union-attr]


def test_authenticated_snapshot_provider_classifies_credential_failure() -> None:
    class FailingCredential:
        def read(self) -> str:
            raise CredentialError("secret-bearing helper diagnostic")

    provider = runtime.AuthenticatedGitHubSnapshotProvider(
        credential=FailingCredential(),  # type: ignore[arg-type]
    )

    with pytest.raises(GitHubAuthenticationError) as raised:
        provider(_policy(), ())

    assert "secret-bearing" not in str(raised.value)


def test_build_controller_wires_exact_runtime_policy_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    captured: dict[str, object] = {}

    def git_environment() -> dict[str, str]:
        return {"GIT_ASKPASS": "/private/helper"}

    github_credential = SimpleNamespace(argv=config.runtime.github_token_command)
    model_credential = SimpleNamespace(argv=config.runtime.codex_api_key_command)

    class FakeCodexDriver:
        def __init__(self, **kwargs: object) -> None:
            captured["codex"] = kwargs
            self.model = str(kwargs["model"])

    class FakeController:
        def __init__(self, **kwargs: object) -> None:
            captured["controller"] = kwargs

    monkeypatch.setattr(runtime, "CodexDriver", FakeCodexDriver)
    monkeypatch.setattr(runtime, "GuardianController", FakeController)
    monkeypatch.setattr(
        runtime,
        "AuthenticatedGitHubSnapshotProvider",
        lambda **kwargs: captured.setdefault("snapshot", kwargs) or object(),
    )
    def resolve_model_key(helper: object) -> str:
        captured["model_helper"] = helper
        return "model-secret"

    monkeypatch.setattr(runtime, "resolve_model_api_key", resolve_model_key)

    controller = runtime._build_controller(
        config=config,
        state=SimpleNamespace(),  # type: ignore[arg-type]
        state_directory=tmp_path,
        github_credential=github_credential,  # type: ignore[arg-type]
        model_credential=model_credential,  # type: ignore[arg-type]
        git_environment=git_environment,
    )

    assert isinstance(controller, FakeController)
    assert captured["codex"] == {
        "model": "gpt-test",
        "reasoning_effort": "high",
        "auth_mode": CodexAuthMode.API_KEY,
        "codex_home": "~/.local/share/localize-guardian/codex",
        "executable": "/opt/bin/codex",
        "timeout_seconds": 120.0,
        "max_attempts": 2,
    }
    controller_kwargs = captured["controller"]
    assert controller_kwargs["snapshot_provider"] is captured["snapshot"]
    assert controller_kwargs["write_broker_factory"] is None
    assert controller_kwargs["prevention_runner"] is None
    assert controller_kwargs["publish_credential_environment"] is git_environment
    assert controller_kwargs["evidence_root"] == tmp_path / "evidence"
    assert controller_kwargs["github_host"] == "github.com"
    assert controller_kwargs["signing_key"] == "A" * 40

    assert controller_kwargs["model_credential_provider"]() == "model-secret"
    assert captured["model_helper"] is model_credential

    revision = ExactRevision(
        host="github.com",
        owner="acme",
        repository="widgets",
        ref="refs/heads/main",
        sha="a" * 40,
    )
    sentinel = object()
    materialize = pytest.MonkeyPatch()
    try:
        materialize.setattr(
            runtime,
            "materialize_exact_checkout",
            lambda incoming, **kwargs: (
                captured.update(checkout_revision=incoming, checkout_kwargs=kwargs)
                or sentinel
            ),
        )
        assert controller_kwargs["checkout_factory"](revision) is sentinel
    finally:
        materialize.undo()
    assert captured["checkout_revision"] == revision
    assert captured["checkout_kwargs"] == {
        "credential_environment": git_environment,
        "git_binary": "/opt/bin/git",
        "signing_program": "/opt/bin/gpg",
        "timeout_seconds": 120.0,
    }


@pytest.mark.parametrize(
    "mode,write_enabled",
    [
        (GuardianMode.OBSERVE, False),
        (GuardianMode.PREPARE, False),
        (GuardianMode.APPLY_OWNED_TRANSLATIONS, True),
        (GuardianMode.PROPOSE_PREVENTION, True),
    ],
)
def test_write_broker_exists_only_for_write_modes(
    mode: GuardianMode,
    write_enabled: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "CodexDriver", lambda **_kwargs: SimpleNamespace(model="x"))
    monkeypatch.setattr(runtime, "GuardianController", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        runtime,
        "AuthenticatedGitHubSnapshotProvider",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        runtime,
        "GitHubWriteBroker",
        lambda **kwargs: captured.setdefault("broker", kwargs) or object(),
    )
    config = _config(mode)
    controller = runtime._build_controller(
        config=config,
        state=SimpleNamespace(),  # type: ignore[arg-type]
        state_directory=tmp_path,
        github_credential=SimpleNamespace(argv=config.runtime.github_token_command),  # type: ignore[arg-type]
        model_credential=None,
        git_environment=lambda: {},
    )

    factory = controller["write_broker_factory"]
    assert (factory is not None) is write_enabled
    if factory is not None:
        broker = factory(_policy())
        assert broker is captured["broker"]
        assert captured["broker"]["base_url"] == "https://api.github.com"
        assert captured["broker"]["token_command"] == (
            "/opt/bin/github-token",
            "read",
        )


def test_propose_mode_wires_credential_separated_prevention_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    config = _config(GuardianMode.PROPOSE_PREVENTION)
    state = SimpleNamespace()

    def git_environment() -> dict[str, str]:
        return {"GIT_ASKPASS": "/private/helper"}

    monkeypatch.setattr(
        runtime,
        "CodexDriver",
        lambda **_kwargs: SimpleNamespace(model="assessment-model"),
    )
    monkeypatch.setattr(runtime, "GuardianController", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        runtime,
        "AuthenticatedGitHubSnapshotProvider",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        runtime,
        "PreventionCodexAuthor",
        lambda **kwargs: captured.setdefault("author", kwargs) or object(),
    )
    monkeypatch.setattr(
        runtime,
        "SandboxedTestRunner",
        lambda **kwargs: captured.setdefault("test_runner", kwargs) or object(),
    )
    coordinator = object()

    def fake_coordinator(**kwargs: object) -> object:
        captured["coordinator"] = kwargs
        return coordinator

    monkeypatch.setattr(runtime, "PreventionCoordinator", fake_coordinator)
    monkeypatch.setattr(
        runtime,
        "PreventionGitHubBroker",
        lambda **kwargs: captured.setdefault("prevention_broker", kwargs) or object(),
    )

    controller = runtime._build_controller(
        config=config,
        state=state,  # type: ignore[arg-type]
        state_directory=tmp_path,
        github_credential=SimpleNamespace(
            argv=config.runtime.github_token_command
        ),  # type: ignore[arg-type]
        model_credential=None,
        git_environment=git_environment,
    )

    assert controller["prevention_runner"] is coordinator
    assert captured["author"] == {
        "model": "gpt-test",
        "reasoning_effort": "high",
        "auth_mode": CodexAuthMode.API_KEY,
        "codex_home": "~/.local/share/localize-guardian/codex",
        "executable": "/opt/bin/codex",
        "timeout_seconds": 120.0,
        "max_attempts": 2,
    }
    assert captured["test_runner"] == {"timeout_seconds": 120.0}
    coordinator_kwargs = captured["coordinator"]
    assert coordinator_kwargs["state"] is state
    assert coordinator_kwargs["publish_credential_environment"] is git_environment
    assert coordinator_kwargs["signing_key"] == "A" * 40
    assert coordinator_kwargs["max_drafts"] == 1
    assert coordinator_kwargs["max_model_calls_per_day"] == 2
    assert coordinator_kwargs["api_billed"] is True
    assert coordinator_kwargs["temporary_root"] == tmp_path

    prevention = config.repositories[0].prevention
    assert prevention is not None
    broker = coordinator_kwargs["broker_factory"](prevention)
    assert broker is captured["prevention_broker"]
    assert captured["prevention_broker"] == {
        "policy": prevention,
        "token_command": ("/opt/bin/github-token", "read"),
        "github_host": "github.com",
        "base_url": "https://api.github.com",
        "timeout_seconds": 30.0,
        "token_command_timeout": 30.0,
    }


def test_run_once_creates_private_state_and_uses_bounded_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    captured: dict[str, object] = {}

    @contextmanager
    def fake_git_environment(command: object, *, temporary_root: Path) -> Iterator[object]:
        captured["github_command"] = command
        captured["temporary_root"] = temporary_root
        yield lambda: {"GIT_ASKPASS": "/private/helper"}

    class FakeController:
        def poll_once(self) -> PollOutcome:
            captured["polled"] = True
            return PollOutcome(lease_acquired=True, repositories_polled=1)

    monkeypatch.setattr(runtime, "git_credential_environment", fake_git_environment)
    monkeypatch.setattr(
        runtime,
        "_build_controller",
        lambda **kwargs: captured.setdefault("controller_kwargs", kwargs)
        and FakeController(),
    )

    previous_umask = os.umask(0o777)
    try:
        result = runtime.run_once(config_path=config_path)
    finally:
        os.umask(previous_umask)

    state_directory = tmp_path / ".guardian"
    state_path = state_directory / "state.sqlite3"
    assert result == 0
    assert captured["polled"] is True
    assert captured["temporary_root"] == state_directory
    assert captured["github_command"].argv == ("gh", "auth", "token")
    assert _mode(state_directory) == 0o700
    assert _mode(state_path) == 0o600
    assert _mode(state_directory / "poll.lock") == 0o600
    assert all(
        _mode(artifact) == 0o600 and artifact.stat().st_nlink == 1
        for artifact in state_directory.glob("state.sqlite3*")
    )


def test_run_once_never_overlaps_a_poll_for_the_same_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    state_directory, _state_path = runtime._prepare_private_state(config_path)
    monkeypatch.setattr(
        runtime,
        "_build_controller",
        lambda **_kwargs: pytest.fail("an overlapping poll must not start"),
    )

    with runtime._exclusive_poll_lock(state_directory):
        assert runtime.run_once(config_path=config_path, scheduled=True) == 0
        with pytest.raises(runtime.GuardianRuntimeError, match="already running"):
            runtime.run_once(config_path=config_path, scheduled=False)

    lock_path = state_directory / "poll.lock"
    assert lock_path.is_file()
    assert _mode(lock_path) == 0o600


def test_prepare_private_state_accepts_a_directory_created_by_a_racer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    state_directory = tmp_path / ".guardian"

    def racing_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        assert path == state_directory
        assert parents is False
        assert exist_ok is False
        os.mkdir(path, mode)
        raise FileExistsError

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    directory, database = runtime._prepare_private_state(config_path)

    assert directory == state_directory
    assert database == state_directory / "state.sqlite3"
    assert _mode(directory) == 0o700


def test_prepare_private_state_secures_a_new_directory_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    previous_umask = os.umask(0o777)
    try:
        directory, _database = runtime._prepare_private_state(config_path)
    finally:
        os.umask(previous_umask)

    assert _mode(directory) == 0o700


def test_prepare_private_state_rejects_a_hardlinked_database(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    database = state_directory / "state.sqlite3"
    database.write_text("", encoding="utf-8")
    database.chmod(0o600)
    (tmp_path / "state-alias").hardlink_to(database)

    directory, returned_database = runtime._prepare_private_state(config_path)

    assert returned_database == database
    with runtime._exclusive_poll_lock(directory):
        with pytest.raises(runtime.GuardianRuntimeError, match="private"):
            runtime._validate_private_state_artifacts(database)


@pytest.mark.skipif(
    not runtime._poll_locking_is_available(),
    reason="POSIX flock is unavailable",
)
def test_first_poll_lock_creation_is_race_safe_across_processes(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    gate = tmp_path / "start"
    ready_paths = tuple(tmp_path / f"ready-{index}" for index in range(2))
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(project_root), existing_pythonpath)
        if value
    )
    script = """
from pathlib import Path
import sys
import time
from localize.guardian import runtime

config_path = Path(sys.argv[1])
gate = Path(sys.argv[2])
ready = Path(sys.argv[3])
contended = Path(sys.argv[4])
ready.touch()
deadline = time.monotonic() + 5
while not gate.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.005)
state_directory, _state_path = runtime._prepare_private_state(config_path)
try:
    with runtime._exclusive_poll_lock(state_directory):
        print("locked", flush=True)
        deadline = time.monotonic() + 5
        while not contended.exists():
            if time.monotonic() >= deadline:
                raise SystemExit(3)
            time.sleep(0.005)
except runtime._GuardianPollAlreadyRunning:
    contended.touch()
    print("contended", flush=True)
"""
    contended = tmp_path / "contended"
    processes = tuple(
        subprocess.Popen(
            (
                sys.executable,
                "-c",
                script,
                str(config_path),
                str(gate),
                str(ready_paths[index]),
                str(contended),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for index in range(2)
    )

    ready_deadline = time.monotonic() + 10
    while not all(path.exists() for path in ready_paths):
        if time.monotonic() >= ready_deadline:
            break
        time.sleep(0.005)
    all_ready = all(path.exists() for path in ready_paths)
    gate.touch()
    results = tuple(process.communicate(timeout=10) for process in processes)

    assert all_ready, results
    assert [process.returncode for process in processes] == [0, 0], results
    assert sorted(stdout.strip() for stdout, _stderr in results) == [
        "contended",
        "locked",
    ]
    assert all(stderr == "" for _stdout, stderr in results)


def test_poll_lock_is_released_when_the_poll_raises(tmp_path: Path) -> None:
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="sentinel"):
        with runtime._exclusive_poll_lock(state_directory):
            raise RuntimeError("sentinel")

    with runtime._exclusive_poll_lock(state_directory):
        pass


def test_poll_lock_creation_secures_mode_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    previous_umask = os.umask(0o777)
    try:
        with runtime._exclusive_poll_lock(state_directory):
            pass
    finally:
        os.umask(previous_umask)

    assert _mode(state_directory / "poll.lock") == 0o600


def test_poll_lock_waits_for_an_exclusive_creator_to_finish_initializing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    lock_path = state_directory / "poll.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o000)
    waits = 0

    def finish_creator(_seconds: float) -> None:
        nonlocal waits
        waits += 1
        lock_path.chmod(0o600)

    monkeypatch.setattr(runtime.time, "sleep", finish_creator)

    with runtime._exclusive_poll_lock(state_directory):
        pass

    assert waits == 1
    assert _mode(lock_path) == 0o600


def test_poll_lock_never_repairs_a_preexisting_unsafe_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    lock_path = state_directory / "poll.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o000)
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))

    with pytest.raises(runtime.GuardianRuntimeError, match="lock"):
        with runtime._exclusive_poll_lock(state_directory):
            pass

    assert _mode(lock_path) == 0o000


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode", "hardlink"])
def test_poll_lock_rejects_unsafe_existing_paths(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    lock_path = state_directory / "poll.lock"
    if unsafe_kind == "symlink":
        target = tmp_path / "target.lock"
        target.write_text("", encoding="utf-8")
        target.chmod(0o600)
        lock_path.symlink_to(target)
    elif unsafe_kind == "hardlink":
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o600)
        (tmp_path / "alias.lock").hardlink_to(lock_path)
    else:
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o644)

    with pytest.raises(runtime.GuardianRuntimeError, match="lock"):
        with runtime._exclusive_poll_lock(state_directory):
            pass


def test_scheduled_run_skips_after_failed_attempt_on_same_day_but_manual_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    state_path = state_directory / "state.sqlite3"
    with GuardianState(state_path) as state:
        state.record_health(
            component="guardian-poll-attempt",
            status="attempted",
            message="attempt",
            checked_at=NOW - timedelta(hours=1),
        )

    calls = 0

    class FakeController:
        def poll_once(self) -> PollOutcome:
            nonlocal calls
            calls += 1
            return PollOutcome(lease_acquired=True)

    @contextmanager
    def fake_credentials(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield lambda: {}

    monkeypatch.setattr(runtime, "_local_now", lambda: NOW)
    monkeypatch.setattr(runtime, "git_credential_environment", fake_credentials)
    monkeypatch.setattr(runtime, "_build_controller", lambda **_kwargs: FakeController())

    assert runtime.run_once(config_path=config_path, scheduled=True) == 0
    assert calls == 0
    assert runtime.run_once(config_path=config_path, scheduled=False) == 0
    assert calls == 1


def test_scheduled_run_catches_up_when_last_attempt_was_previous_local_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    state_directory = tmp_path / ".guardian"
    state_directory.mkdir(mode=0o700)
    with GuardianState(state_directory / "state.sqlite3") as state:
        state.record_health(
            component="guardian-poll-attempt",
            status="attempted",
            message="attempt",
            checked_at=NOW - timedelta(days=1),
        )

    calls = 0

    class FakeController:
        def poll_once(self) -> PollOutcome:
            nonlocal calls
            calls += 1
            return PollOutcome(lease_acquired=True)

    @contextmanager
    def fake_credentials(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield lambda: {}

    monkeypatch.setattr(runtime, "_local_now", lambda: NOW)
    monkeypatch.setattr(runtime, "git_credential_environment", fake_credentials)
    monkeypatch.setattr(runtime, "_build_controller", lambda **_kwargs: FakeController())

    assert runtime.run_once(config_path=config_path, scheduled=True) == 0
    assert calls == 1


def test_operator_pipeline_config_is_snapshotted_before_controller_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    sequence: list[str] = []

    @contextmanager
    def fake_snapshot(**_kwargs: object) -> Iterator[dict[str, PipelineConfigSnapshot]]:
        sequence.append("snapshot-enter")
        try:
            yield {
                "acme/widgets": PipelineConfigSnapshot(
                    config_root=tmp_path,
                    config_path=config_path,
                    bundle_digest="d" * 64,
                )
            }
        finally:
            sequence.append("snapshot-exit")

    @contextmanager
    def fake_credentials(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield lambda: {}

    class FakeController:
        def poll_once(self) -> PollOutcome:
            sequence.append("github-model-poll")
            return PollOutcome(lease_acquired=True)

    monkeypatch.setattr(
        runtime,
        "_snapshot_operator_pipeline_configs",
        fake_snapshot,
    )
    monkeypatch.setattr(runtime, "git_credential_environment", fake_credentials)
    monkeypatch.setattr(runtime, "_build_controller", lambda **_kwargs: FakeController())

    assert runtime.run_once(config_path=config_path) == 0
    assert sequence == ["snapshot-enter", "github-model-poll", "snapshot-exit"]
    with GuardianState(tmp_path / ".guardian/state.sqlite3") as state:
        audit = state.latest_health("pipeline-config")
    assert audit is not None
    assert audit.details == {
        "repository": "acme/widgets",
        "bundle_digest": "d" * 64,
    }


def test_failed_scheduled_poll_is_not_retried_until_manual_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    calls = 0

    class FailingController:
        def poll_once(self) -> PollOutcome:
            nonlocal calls
            calls += 1
            return PollOutcome(
                lease_acquired=True,
                runs_failed=1,
                failures=("safe-failure-type",),
            )

    @contextmanager
    def fake_credentials(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield lambda: {}

    monkeypatch.setattr(runtime, "_local_now", lambda: NOW)
    monkeypatch.setattr(runtime, "git_credential_environment", fake_credentials)
    monkeypatch.setattr(runtime, "_build_controller", lambda **_kwargs: FailingController())

    assert runtime.run_once(config_path=config_path, scheduled=True) == 1
    assert runtime.run_once(config_path=config_path, scheduled=True) == 0
    assert calls == 1
    assert runtime.run_once(config_path=config_path, scheduled=False) == 1
    assert calls == 2

    with GuardianState(tmp_path / ".guardian/state.sqlite3") as state:
        attempt = state.latest_health("guardian-poll-attempt")
    assert attempt is not None
    assert attempt.status == "attempted"
    assert attempt.details == {
        "scheduled": False,
        "local_date": NOW.date().isoformat(),
        "local_minute": NOW.hour * 60 + NOW.minute,
    }


def test_model_capacity_circuit_is_a_failed_runtime_outcome() -> None:
    assert runtime._exit_code(
        PollOutcome(lease_acquired=True, model_circuit_open=True)
    ) == 1


def test_scheduled_due_uses_local_day_instead_of_utc_day() -> None:
    local_zone = timezone(timedelta(hours=14))
    now = datetime(2026, 8, 30, 1, 0, tzinfo=local_zone)
    # The UTC date is still August 29, but this success occurred at 00:30 on
    # August 30 in the scheduler's local time zone.
    attempt = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)
    state = SimpleNamespace(
        latest_health=lambda _component: SimpleNamespace(
            status="attempted",
            checked_at=attempt,
        )
    )

    assert runtime._scheduled_poll_is_due(
        state,
        now=now,
        schedule=GuardianSchedule(),
    ) is False


def test_scheduled_due_does_not_repeat_across_dst_fallback() -> None:
    now = datetime(2026, 10, 25, 0, 15, tzinfo=timezone(timedelta(hours=1)))
    prior = datetime(2026, 10, 25, 0, 5, tzinfo=timezone(timedelta(hours=2)))
    state = SimpleNamespace(
        latest_health=lambda _component: SimpleNamespace(
            status="attempted",
            checked_at=prior,
            details={
                "scheduled": True,
                "local_date": "2026-10-25",
                "local_minute": 5,
            },
        )
    )

    assert runtime._scheduled_poll_is_due(
        state,
        now=now,
        schedule=GuardianSchedule(hour=0, minute=0),
    ) is False


def test_manual_poll_before_schedule_does_not_suppress_scheduled_poll() -> None:
    now = datetime(2026, 8, 30, 8, 7, tzinfo=timezone.utc)
    manual = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)
    state = SimpleNamespace(
        latest_health=lambda _component: SimpleNamespace(
            status="attempted",
            checked_at=manual,
            details={
                "scheduled": False,
                "local_date": "2026-08-30",
                "local_minute": 7 * 60,
            },
        )
    )

    assert runtime._scheduled_poll_is_due(
        state,
        now=now,
        schedule=GuardianSchedule(hour=8, minute=7),
    ) is True


def test_scheduled_due_uses_configured_local_clock() -> None:
    now = datetime(2026, 8, 30, 5, 14, tzinfo=timezone(timedelta(hours=2)))
    state = SimpleNamespace(latest_health=lambda _component: None)

    assert runtime._scheduled_poll_is_due(
        state,
        now=now,
        schedule=GuardianSchedule(hour=5, minute=15),
    ) is False
    assert runtime._scheduled_poll_is_due(
        state,
        now=now + timedelta(minutes=1),
        schedule=GuardianSchedule(hour=5, minute=15),
    ) is True


def test_run_once_uses_configured_schedule_for_due_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    config_path.write_text(
        "schedule: {hour: 13, minute: 0}\n"
        + config_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "_local_now", lambda: NOW)
    monkeypatch.setattr(
        runtime,
        "_build_controller",
        lambda **_kwargs: pytest.fail("controller must not run before 13:00"),
    )

    assert runtime.run_once(config_path=config_path, scheduled=True) == 0


def test_run_once_redacts_setup_and_poll_exception_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    secret = "never-print-this-token"

    @contextmanager
    def fake_credentials(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield lambda: {}

    class FailingController:
        def poll_once(self) -> PollOutcome:
            raise RuntimeError(secret)

    monkeypatch.setattr(runtime, "git_credential_environment", fake_credentials)
    monkeypatch.setattr(runtime, "_build_controller", lambda **_kwargs: FailingController())

    with pytest.raises(runtime.GuardianRuntimeError) as error:
        runtime.run_once(config_path=config_path)

    assert secret not in str(error.value)
    assert error.value.__cause__ is None


def test_run_once_rejects_invalid_config_without_echoing_untrusted_values(
    tmp_path: Path,
) -> None:
    secret = "never-print-this-config-value"
    config_path = tmp_path / "guardian.yaml"
    config_path.write_text(
        f"repositories: []\nunknown_secret: {secret}\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    with pytest.raises(runtime.GuardianRuntimeError) as error:
        runtime.run_once(config_path=config_path)

    assert secret not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("unsafe_kind", ["group-writable", "symlink"])
def test_runtime_rejects_untrusted_config_ancestor_paths(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir(mode=0o700)
    if unsafe_kind == "symlink":
        config_directory = tmp_path / "operator"
        config_directory.symlink_to(trusted, target_is_directory=True)
    else:
        config_directory = trusted
        config_directory.chmod(0o770)
    config_path = config_directory / "guardian.yaml"
    _write_minimal_config(config_path)

    with pytest.raises(runtime.GuardianRuntimeError, match="unsafe"):
        runtime.run_once(config_path=config_path)

    assert not (trusted / ".guardian").exists()


def test_run_once_requires_explicit_signing_key_before_write_mode_setup(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "mode: observe",
            "mode: apply-owned-translations",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(runtime.GuardianRuntimeError, match="signing key"):
        runtime.run_once(config_path=config_path)

    assert not (tmp_path / ".guardian").exists()


def test_run_once_rejects_symlinked_or_non_private_state_paths(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "guardian.yaml"
    _write_minimal_config(config_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".guardian").symlink_to(target, target_is_directory=True)

    with pytest.raises(runtime.GuardianRuntimeError, match="private"):
        runtime.run_once(config_path=config_path)

    (tmp_path / ".guardian").unlink()
    (tmp_path / ".guardian").mkdir(mode=0o755)
    os.chmod(tmp_path / ".guardian", 0o755)
    with pytest.raises(runtime.GuardianRuntimeError, match="private"):
        runtime.run_once(config_path=config_path)
