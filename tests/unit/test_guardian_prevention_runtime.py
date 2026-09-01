from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterator

import httpx
import pytest

from localize.guardian import prevention_runtime
from localize.guardian.codex import (
    CodexAuthenticationError,
    CodexCapacityError,
    CodexUsage,
)
from localize.guardian.credentials import CredentialError
from localize.guardian.github import GitHubAuthenticationError
from localize.guardian.models import (
    AllowedHeadRepository,
    CodexAuthMode,
    ExactRepository,
    GuardianMode,
    PreventionPolicy,
    RecurrenceCandidate,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.prevention import TestCommandResult, TestOutcome
from localize.guardian.prevention_runtime import (
    PreventionAuthorResult,
    PreventionBaseSnapshot,
    PreventionCodexAuthor,
    PreventionCoordinator,
    PreventionDraftResult,
    PreventionGitHubBroker,
    PreventionRuntimeError,
    SandboxedTestRunner,
)
from localize.guardian.state import GuardianState
from localize.guardian.workspace import (
    CommitResult,
    ExactRevision,
    PreventionPublicationResult,
)


UTC = timezone.utc
BASE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
TOKEN = "github-token-never-log"


def _live_lease() -> None:
    return None


@pytest.fixture
def stub_network_canaries(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def canaries() -> Iterator[tuple[str, int, str]]:
        yield "127.0.0.1", 43210, "/private/guardian-canary.sock"

    monkeypatch.setattr(prevention_runtime, "_network_canaries", canaries)


def _prevention_policy(*, private_target_opt_in: bool = False) -> PreventionPolicy:
    return PreventionPolicy(
        target_repository=ExactRepository(full_name="guardian/pipeline", id=101),
        target_base_branch="main",
        push_repository=ExactRepository(full_name="guardian/pipeline", id=101),
        push_branch_prefix="guardian/prevention-",
        allowed_code_path_globs=("localize/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(
            ("/opt/localize-guardian/bin/pytest", "tests/unit/test_rules.py", "-q"),
        ),
        sandbox_argv_prefix=("/usr/bin/sandbox-tool", "--profile", "/safe/profile"),
        max_changed_files=4,
        max_changed_bytes=16_384,
        private_target_model_opt_in=private_target_opt_in,
    )


def _repository_policy(
    *,
    private_opt_in: bool = False,
    private_target_opt_in: bool = False,
) -> RepositoryPolicy:
    actor = TrustedActor(login="translation-bot", id=7, type="User")
    owner = TrustedActor(login="translation-bot", id=8, type="Organization")
    return RepositoryPolicy(
        base_repo="acme/translations",
        base_repo_id=42,
        base_branch="main",
        allowed_pr_authors=(actor,),
        allowed_head_owners=(owner,),
        allowed_head_repositories=(
            AllowedHeadRepository(full_name="translation-bot/translations", id=84),
        ),
        allowed_branch_globs=("translation-*",),
        allowed_path_globs=("l10n/*.properties",),
        pipeline_config_path="config.yaml",
        source_locale="en",
        trusted_reviewers={"ru": (TrustedActor("reviewer", 9, "User"),)},
        trusted_bots={},
        private_repo_model_opt_in=private_opt_in,
        prevention=_prevention_policy(private_target_opt_in=private_target_opt_in),
    )


def _base_revision() -> ExactRevision:
    return ExactRevision(
        host="github.com",
        owner="guardian",
        repository="pipeline",
        ref="refs/heads/main",
        sha=BASE_SHA,
    )


def _repo_payload() -> dict[str, object]:
    return {
        "id": 101,
        "full_name": "guardian/pipeline",
        "private": False,
    }


def _branch_payload(name: str, sha: str) -> dict[str, object]:
    return {"name": name, "commit": {"sha": sha}}


def _pull_payload(
    *,
    number: int,
    branch: str,
    body: str,
    draft: bool,
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.test/guardian/pipeline/pull/{number}",
        "body": body,
        "draft": draft,
        "head": {
            "sha": CANDIDATE_SHA,
            "ref": branch,
            "repo": {"id": 101, "full_name": "guardian/pipeline"},
        },
        "base": {
            "sha": BASE_SHA,
            "ref": "main",
            "repo": {"id": 101, "full_name": "guardian/pipeline"},
        },
    }


def _response(
    request: httpx.Request, payload: object, status: int = 200
) -> httpx.Response:
    return httpx.Response(status, request=request, json=payload)


def test_prevention_author_uses_workspace_write_stdin_and_scrubs_write_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = list(argv)
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                    "cost_usd": 0.02,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "forbidden-github")
    monkeypatch.setenv("GH_TOKEN", "forbidden-gh")
    monkeypatch.setenv("GIT_ASKPASS", "/forbidden/askpass")
    monkeypatch.setenv("GNUPGHOME", "/forbidden/gnupg")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-model-key")
    author = PreventionCodexAuthor(
        model="gpt-5.6-sol",
        reasoning_effort="max",
        auth_mode=CodexAuthMode.API_KEY,
        timeout_seconds=17,
    )
    result = author.run(
        workspace=workspace,
        scope="pipeline_code",
        summary="Preserve indexed placeholders in the validator",
        evidence_feedback_ids=("review_comment:42:revision-7",),
        policy=_prevention_policy(),
        api_key="explicit-model-key",
    )

    argv = observed["argv"]
    assert isinstance(argv, list)
    assert "--sandbox" not in argv
    assert argv[1:3] == ["--ask-for-approval", "never"]
    assert 'default_permissions="guardian_prevention_author"' in argv
    assert (
        'permissions.guardian_prevention_author.filesystem={":minimal"="read",'
        '":workspace_roots"={"."="write"}}'
    ) in argv
    assert "--ignore-rules" in argv
    assert "--ignore-user-config" in argv
    assert argv[-1] == "-"
    kwargs = observed["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["input"].startswith("You are preparing")
    assert "Preserve indexed placeholders" in kwargs["input"]
    assert "Preserve indexed placeholders" not in argv
    assert kwargs["timeout"] == 17
    assert kwargs["limits"].require_linux_cgroup is True
    assert kwargs["env"]["CODEX_API_KEY"] == "explicit-model-key"
    assert "OPENAI_API_KEY" not in kwargs["env"]
    for forbidden in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS", "GNUPGHOME"):
        assert forbidden not in kwargs["env"]
    assert result.usage == CodexUsage(input_tokens=12, output_tokens=3, cost_usd=0.02)


def test_prevention_author_opens_auth_circuit_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="401 Unauthorized explicit-secret-value",
        )

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)
    with pytest.raises(CodexAuthenticationError) as failure:
        PreventionCodexAuthor(
            model="gpt-5.6-sol",
            reasoning_effort="max",
            auth_mode=CodexAuthMode.API_KEY,
        ).run(
            workspace=workspace,
            scope="pipeline_code",
            summary="Regression",
            evidence_feedback_ids=("review:1:revision-1",),
            policy=_prevention_policy(),
            api_key="explicit-secret-value",
        )
    assert "explicit-secret-value" not in str(failure.value)


def test_prevention_author_never_inherits_a_host_model_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed_environment: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)
    monkeypatch.setenv("CODEX_API_KEY", "inherited-codex-key")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-openai-key")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)

    PreventionCodexAuthor(
        model="gpt-5.6-sol",
        reasoning_effort="max",
        auth_mode=CodexAuthMode.CHATGPT,
        codex_home=codex_home,
    ).run(
        workspace=workspace,
        scope="pipeline_code",
        summary="Regression",
        evidence_feedback_ids=("review:1:revision-1",),
        policy=_prevention_policy(),
        api_key=None,
    )

    assert "CODEX_API_KEY" not in observed_environment
    assert "OPENAI_API_KEY" not in observed_environment
    assert observed_environment["CODEX_HOME"] == str(codex_home.resolve())


def test_sandboxed_runner_uses_identical_configured_argv_and_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_network_canaries: None,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []
    returncodes = iter((0, 1, 0, 0))

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, next(returncodes))

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)
    cgroup_parent_procs = Path("/sys/fs/cgroup/guardian/cgroup.procs")
    monkeypatch.setattr(
        prevention_runtime,
        "linux_cgroup_parent_procs",
        lambda: cgroup_parent_procs,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "forbidden")
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden")
    results = SandboxedTestRunner(timeout_seconds=23).run_pair(
        base_workspace=base,
        candidate_workspace=candidate,
        policy=_prevention_policy(),
        base_sha=BASE_SHA,
        candidate_sha=CANDIDATE_SHA,
        test_overlay_hash="c" * 64,
    )

    expected = [
        "/usr/bin/sandbox-tool",
        "--profile",
        "/safe/profile",
        "/opt/localize-guardian/bin/pytest",
        "tests/unit/test_rules.py",
        "-q",
    ]
    sandbox_prefix = list(_prevention_policy().sandbox_argv_prefix)
    prefix_length = len(sandbox_prefix)
    assert [calls[1][0], calls[3][0]] == [expected, expected]
    assert calls[0][0][:prefix_length] == sandbox_prefix
    assert calls[2][0][:prefix_length] == sandbox_prefix
    assert calls[0][0][-1] == str(cgroup_parent_procs)
    assert calls[2][0][-1] == str(cgroup_parent_procs)
    assert "-I" in calls[0][0]
    assert "-c" in calls[0][0]
    assert [calls[1][1]["cwd"], calls[3][1]["cwd"]] == [base, candidate]
    for _argv, kwargs in calls:
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 23
        assert kwargs["limits"].require_linux_cgroup is True
        assert "GITHUB_TOKEN" not in kwargs["env"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
    assert [result.outcome for result in results] == [
        TestOutcome.FAILED,
        TestOutcome.PASSED,
    ]
    assert results[0].argv == results[1].argv
    assert results[0].test_overlay_hash == results[1].test_overlay_hash


@pytest.mark.parametrize("returncodes", [(0, 0), (2, 0), (1, 1)])
def test_sandboxed_runner_rejects_false_regression_proofs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncodes: tuple[int, int],
    stub_network_canaries: None,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    codes = iter((0, returncodes[0], 0, returncodes[1]))
    monkeypatch.setattr(
        prevention_runtime,
        "run_bounded_process",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, next(codes)),
    )
    with pytest.raises(
        prevention_runtime.PreventionPolicyError, match="every configured"
    ):
        SandboxedTestRunner(timeout_seconds=10).run_pair(
            base_workspace=base,
            candidate_workspace=candidate,
            policy=_prevention_policy(),
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            test_overlay_hash="c" * 64,
        )


def test_sandboxed_runner_rejects_prefix_that_fails_confinement_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_network_canaries: None,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    calls = 0

    def fake_run(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)

    with pytest.raises(
        prevention_runtime.PreventionPolicyError,
        match="confinement probe",
    ):
        SandboxedTestRunner(timeout_seconds=10).run_pair(
            base_workspace=base,
            candidate_workspace=candidate,
            policy=_prevention_policy(),
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            test_overlay_hash="c" * 64,
        )

    assert calls == 1


@pytest.mark.parametrize(
    "policy",
    [
        replace(
            _prevention_policy(),
            focused_test_argv=(("venv/bin/pytest", "tests/unit/test_rules.py"),),
        ),
        replace(
            _prevention_policy(),
            sandbox_argv_prefix=("sandbox-tool", "--profile", "/safe/profile"),
        ),
    ],
)
def test_sandboxed_runner_rejects_non_absolute_executables_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: PreventionPolicy,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    subprocess_calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal subprocess_calls
        subprocess_calls += 1
        raise AssertionError("an unsafe executable must never run")

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)

    with pytest.raises(prevention_runtime.PreventionPolicyError, match="absolute"):
        SandboxedTestRunner(timeout_seconds=10).run_pair(
            base_workspace=base,
            candidate_workspace=candidate,
            policy=policy,
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            test_overlay_hash="c" * 64,
        )

    assert subprocess_calls == 0


def test_sandboxed_runner_rejects_prefix_that_allows_outbound_connect(
    tmp_path: Path,
    stub_network_canaries: None,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    wrapper = tmp_path / "deny-bind-only.py"
    wrapper.write_text(
        """
import pathlib
import socket
import sys

command = sys.argv[1:]
if len(command) < 4 or command[1:3] != ["-I", "-c"]:
    raise SystemExit(97)

def deny_bytes(*_args, **_kwargs):
    raise PermissionError("outside filesystem access denied")

pathlib.Path.read_bytes = deny_bytes
pathlib.Path.write_bytes = deny_bytes

real_socket = socket.socket

class DenyBindSocket:
    def __init__(self, *args, **kwargs):
        self._socket = real_socket(*args, **kwargs)

    def bind(self, *_args, **_kwargs):
        raise PermissionError("listener denied")

    def connect(self, *_args, **_kwargs):
        return None

    def __getattr__(self, name):
        return getattr(self._socket, name)

socket.socket = DenyBindSocket
sys.argv = ["-c", *command[4:]]
exec(compile(command[3], "<guardian-probe>", "exec"), {"__name__": "__main__"})
""".lstrip(),
        encoding="utf-8",
    )
    policy = replace(
        _prevention_policy(),
        sandbox_argv_prefix=(sys.executable, str(wrapper)),
    )

    with pytest.raises(
        prevention_runtime.PreventionPolicyError,
        match="confinement probe",
    ):
        SandboxedTestRunner(timeout_seconds=10).run_pair(
            base_workspace=base,
            candidate_workspace=candidate,
            policy=policy,
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            test_overlay_hash="c" * 64,
        )


def test_sandbox_probe_accepts_sandbox_that_denies_socket_creation(
    tmp_path: Path,
    stub_network_canaries: None,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    wrapper = tmp_path / "deny-all-sockets.py"
    wrapper.write_text(
        """
import pathlib
import socket
import sys

command = sys.argv[1:]
if len(command) < 4 or command[1:3] != ["-I", "-c"]:
    raise SystemExit(97)

def deny_bytes(*_args, **_kwargs):
    raise PermissionError("outside filesystem access denied")

def deny_socket(*_args, **_kwargs):
    raise PermissionError("all socket creation denied")

pathlib.Path.read_bytes = deny_bytes
pathlib.Path.write_bytes = deny_bytes
socket.socket = deny_socket
sys.argv = ["-c", *command[4:]]
exec(compile(command[3], "<guardian-probe>", "exec"), {"__name__": "__main__"})
""".lstrip(),
        encoding="utf-8",
    )
    runner = SandboxedTestRunner(timeout_seconds=10)

    runner._prove_confinement(
        workspace=workspace,
        private=private,
        sandbox_prefix=(sys.executable, str(wrapper)),
        environment=runner._environment(home=private, temp=private),
    )


def test_sandboxed_runner_fails_closed_if_parent_cannot_create_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    subprocess_calls = 0

    def deny_listener(*_args, **_kwargs):
        raise PermissionError("listener capability denied")

    def fake_run(*_args, **_kwargs):
        nonlocal subprocess_calls
        subprocess_calls += 1
        raise AssertionError("target code must not run without a live canary")

    monkeypatch.setattr(prevention_runtime.socket, "socket", deny_listener)
    monkeypatch.setattr(prevention_runtime, "run_bounded_process", fake_run)

    with pytest.raises(
        prevention_runtime.PreventionPolicyError,
        match="confinement probe",
    ):
        SandboxedTestRunner(timeout_seconds=10).run_pair(
            base_workspace=base,
            candidate_workspace=candidate,
            policy=_prevention_policy(),
            base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            test_overlay_hash="c" * 64,
        )

    assert subprocess_calls == 0


def test_github_broker_revalidates_numeric_identities_and_opens_draft_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch = "guardian/prevention-" + "d" * 64
    evidence_hash = "d" * 64
    marker = PreventionGitHubBroker._marker(evidence_hash, CANDIDATE_SHA)
    requests: list[tuple[str, str, object]] = []
    lease_checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path == "/repos/guardian/pipeline":
            return _response(request, _repo_payload())
        if path == "/repos/guardian/pipeline/branches/main":
            return _response(request, _branch_payload("main", BASE_SHA))
        if path == f"/repos/guardian/pipeline/branches/{branch}":
            return _response(request, _branch_payload(branch, CANDIDATE_SHA))
        if path == "/repos/guardian/pipeline/pulls" and request.method == "GET":
            return _response(request, [])
        if path == "/repos/guardian/pipeline/pulls" and request.method == "POST":
            assert lease_checks == 2
            payload = json.loads(request.content)
            assert payload["draft"] is True
            assert payload["maintainer_can_modify"] is False
            assert payload["head"] == f"guardian:{branch}"
            assert marker in payload["body"]
            return _response(
                request,
                _pull_payload(
                    number=17,
                    branch=branch,
                    body=payload["body"],
                    draft=True,
                ),
                status=201,
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    monkeypatch.setattr(
        prevention_runtime.SecretCommand,
        "read",
        lambda _self: TOKEN,
    )
    broker = PreventionGitHubBroker(
        policy=_prevention_policy(),
        token_command=("credential-helper",),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    base = broker.capture_base()
    assert base.revision == _base_revision()
    assert base.target_repository_id == 101
    broker.verify_publish_authority(
        expected_base_sha=BASE_SHA,
        branch=branch,
        candidate_sha=CANDIDATE_SHA,
    )

    def lost_lease() -> None:
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        broker.open_draft(
            branch=branch,
            expected_base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            evidence_hash=evidence_hash,
            title="Prevent recurrence: placeholder parity",
            body="Validated body\n",
            before_create=lost_lease,
        )
    assert not any(method == "POST" for method, _path, _body in requests)
    requests.clear()

    def before_create() -> None:
        nonlocal lease_checks
        lease_checks += 1

    draft = broker.open_draft(
        branch=branch,
        expected_base_sha=BASE_SHA,
        candidate_sha=CANDIDATE_SHA,
        evidence_hash=evidence_hash,
        title="Prevent recurrence: placeholder parity",
        body="Validated body\n",
        before_create=before_create,
    )

    assert draft == PreventionDraftResult(
        number=17,
        html_url="https://github.test/guardian/pipeline/pull/17",
        candidate_sha=CANDIDATE_SHA,
        created=True,
    )
    assert any(method == "POST" for method, _path, _body in requests)
    assert lease_checks == 2
    assert all(TOKEN.encode() not in body for _method, _path, body in requests)


def test_github_broker_recovers_exact_existing_guardian_pr_without_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch = "guardian/prevention-" + "e" * 64
    evidence_hash = "e" * 64
    marker = PreventionGitHubBroker._marker(evidence_hash, CANDIDATE_SHA)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        path = request.url.path
        if path == "/repos/guardian/pipeline":
            return _response(request, _repo_payload())
        if path == "/repos/guardian/pipeline/branches/main":
            return _response(request, _branch_payload("main", BASE_SHA))
        if path == f"/repos/guardian/pipeline/branches/{branch}":
            return _response(request, _branch_payload(branch, CANDIDATE_SHA))
        if path == "/repos/guardian/pipeline/pulls":
            return _response(
                request,
                [
                    _pull_payload(
                        number=18,
                        branch=branch,
                        body=f"{marker}\nValidated body\n",
                        draft=False,
                    )
                ],
            )
        raise AssertionError(path)

    monkeypatch.setattr(prevention_runtime.SecretCommand, "read", lambda _self: TOKEN)
    broker = PreventionGitHubBroker(
        policy=_prevention_policy(),
        token_command=("credential-helper",),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    draft = broker.open_draft(
        branch=branch,
        expected_base_sha=BASE_SHA,
        candidate_sha=CANDIDATE_SHA,
        evidence_hash=evidence_hash,
        title="ignored for recovery",
        body="Validated body\n",
        before_create=lambda: pytest.fail(
            "existing pull recovery must not request a mutation lease"
        ),
    )
    assert draft.created is False
    assert draft.number == 18
    assert "POST" not in methods


def test_github_broker_revalidates_base_after_pagination_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch = "guardian/prevention-" + "f" * 64
    evidence_hash = "f" * 64
    base_reads = 0
    methods: list[str] = []
    lease_checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal base_reads
        methods.append(request.method)
        path = request.url.path
        if path == "/repos/guardian/pipeline":
            return _response(request, _repo_payload())
        if path == "/repos/guardian/pipeline/branches/main":
            base_reads += 1
            sha = BASE_SHA if base_reads == 1 else "f" * 40
            return _response(request, _branch_payload("main", sha))
        if path == f"/repos/guardian/pipeline/branches/{branch}":
            return _response(request, _branch_payload(branch, CANDIDATE_SHA))
        if path == "/repos/guardian/pipeline/pulls" and request.method == "GET":
            return _response(request, [])
        raise AssertionError(f"unexpected {request.method} {path}")

    def before_create() -> None:
        nonlocal lease_checks
        lease_checks += 1

    monkeypatch.setattr(prevention_runtime.SecretCommand, "read", lambda _self: TOKEN)
    broker = PreventionGitHubBroker(
        policy=_prevention_policy(),
        token_command=("credential-helper",),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PreventionRuntimeError, match="base moved"):
        broker.open_draft(
            branch=branch,
            expected_base_sha=BASE_SHA,
            candidate_sha=CANDIDATE_SHA,
            evidence_hash=evidence_hash,
            title="Prevent recurrence: placeholder parity",
            body="Validated body\n",
            before_create=before_create,
        )

    assert lease_checks == 1
    assert "POST" not in methods


def test_github_broker_fails_closed_on_repository_id_or_base_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_ok = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/guardian/pipeline":
            payload = _repo_payload()
            payload["id"] = 101 if identity_ok["value"] else 999
            return _response(request, payload)
        if request.url.path == "/repos/guardian/pipeline/branches/main":
            return _response(request, _branch_payload("main", "f" * 40))
        raise AssertionError(request.url.path)

    monkeypatch.setattr(prevention_runtime.SecretCommand, "read", lambda _self: TOKEN)
    broker = PreventionGitHubBroker(
        policy=_prevention_policy(),
        token_command=("credential-helper",),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PreventionRuntimeError, match="identity"):
        broker.capture_base()
    identity_ok["value"] = True
    with pytest.raises(PreventionRuntimeError, match="base moved"):
        broker.verify_publish_authority(
            expected_base_sha=BASE_SHA,
            branch="guardian/prevention-" + "f" * 64,
            candidate_sha=CANDIDATE_SHA,
        )


@pytest.mark.parametrize("status", [401, 403])
def test_prevention_github_authentication_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setattr(prevention_runtime.SecretCommand, "read", lambda _self: TOKEN)
    broker = PreventionGitHubBroker(
        policy=_prevention_policy(),
        token_command=("credential-helper",),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(
            lambda request: _response(request, {}, status=status)
        ),
    )

    with pytest.raises(GitHubAuthenticationError):
        broker.capture_base()


def test_prevention_github_credential_helper_failure_is_typed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_credential(_self) -> str:
        raise CredentialError("explicit-secret-value")

    monkeypatch.setattr(prevention_runtime.SecretCommand, "read", fail_credential)
    broker = PreventionGitHubBroker(
        policy=_prevention_policy(),
        token_command=("credential-helper",),
        base_url="https://api.github.test",
    )

    with pytest.raises(GitHubAuthenticationError) as failure:
        broker.capture_base()

    assert "explicit-secret-value" not in str(failure.value)


class _FakeWorkspace:
    def __init__(
        self,
        path: Path,
        revision: ExactRevision,
        broker: "_FakeBroker",
        checkout_factory: "_FakeCheckoutFactory",
    ) -> None:
        self.path = path
        self.revision = revision
        self.broker = broker
        self.checkout_factory = checkout_factory
        self.published = False

    def commit_prevention_changes(self, *, expected_paths, evidence_hash, **_kwargs):
        assert set(expected_paths) == {"localize/rules.py", "tests/unit/test_rules.py"}
        assert len(evidence_hash) == 64
        return CommitResult(
            commit_sha=CANDIDATE_SHA,
            parent_sha=BASE_SHA,
            changed_paths=tuple(sorted(expected_paths)),
            signature_verified=True,
        )

    def publish_prevention_branch(self, commit, **kwargs):
        kwargs["before_push"]()
        self.published = True
        self.checkout_factory.publications += 1
        self.broker.branch_shas[kwargs["branch"]] = commit.commit_sha
        assert kwargs["push_repository"] == "guardian/pipeline"
        assert kwargs["branch"].startswith("guardian/prevention-")
        return PreventionPublicationResult(
            repository="guardian/pipeline",
            ref=f"refs/heads/{kwargs['branch']}",
            commit_sha=commit.commit_sha,
            created=True,
        )


class _FakeCheckoutFactory:
    def __init__(self, base_tree: Path, root: Path, broker: "_FakeBroker") -> None:
        self.base_tree = base_tree
        self.root = root
        self.broker = broker
        self.calls = 0
        self.publications = 0

    @contextmanager
    def __call__(self, revision: ExactRevision) -> Iterator[_FakeWorkspace]:
        self.calls += 1
        target = self.root / f"checkout-{self.calls}"
        shutil.copytree(self.base_tree, target)
        yield _FakeWorkspace(target, revision, self.broker, self)


class _FakeAuthor:
    model = "gpt-test"
    max_attempts = 1

    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, workspace: Path, **_kwargs) -> PreventionAuthorResult:
        self.calls += 1
        (workspace / "localize/rules.py").write_text(
            "def preserve(value):\n    return value.strip()\n",
            encoding="utf-8",
        )
        (workspace / "tests/unit/test_rules.py").write_text(
            "def test_preserve():\n    assert preserve(' value ') == 'value'\n",
            encoding="utf-8",
        )
        return PreventionAuthorResult(
            attempts=1,
            usage=CodexUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
        )


class _FakeTestRunner:
    def run_pair(
        self,
        *,
        policy: PreventionPolicy,
        base_sha: str,
        candidate_sha: str,
        test_overlay_hash: str,
        **_kwargs,
    ) -> tuple[TestCommandResult, ...]:
        argv = policy.focused_test_argv[0]
        return (
            TestCommandResult(
                phase="base",
                outcome=TestOutcome.FAILED,
                argv=argv,
                commit_sha=base_sha,
                parent_sha=None,
                returncode=1,
                test_overlay_hash=test_overlay_hash,
            ),
            TestCommandResult(
                phase="patched",
                outcome=TestOutcome.PASSED,
                argv=argv,
                commit_sha=candidate_sha,
                parent_sha=base_sha,
                returncode=0,
                test_overlay_hash=test_overlay_hash,
            ),
        )


class _FakeBroker:
    def __init__(self, *, private: bool = False) -> None:
        self.private = private
        self.branch_shas: dict[str, str] = {}
        self.capture_calls = 0
        self.verify_calls = 0
        self.open_calls = 0

    def capture_base(self) -> PreventionBaseSnapshot:
        self.capture_calls += 1
        return PreventionBaseSnapshot(
            revision=_base_revision(),
            target_repository_id=101,
            push_repository_id=101,
            private=self.private,
        )

    def branch_sha(self, branch: str) -> str | None:
        return self.branch_shas.get(branch)

    def verify_publish_authority(self, **_kwargs) -> None:
        self.verify_calls += 1
        return None

    def open_draft(self, *, branch: str, candidate_sha: str, **_kwargs):
        _kwargs["before_create"]()
        self.open_calls += 1
        self.branch_shas[branch] = candidate_sha
        return PreventionDraftResult(
            number=20 + self.open_calls,
            html_url=f"https://github.test/guardian/pipeline/pull/{20 + self.open_calls}",
            candidate_sha=candidate_sha,
            created=True,
        )


def _base_tree(tmp_path: Path) -> Path:
    base = tmp_path / "base-tree"
    (base / "localize").mkdir(parents=True)
    (base / "tests/unit").mkdir(parents=True)
    (base / "localize/rules.py").write_text(
        "def preserve(value):\n    return value\n",
        encoding="utf-8",
    )
    (base / "tests/unit/test_rules.py").write_text(
        "def test_preserve():\n    assert True\n",
        encoding="utf-8",
    )
    return base


def _candidate(summary: str = "Placeholder validation omitted one token family"):
    return RecurrenceCandidate(
        scope="pipeline_code",
        summary=summary,
        evidence_feedback_ids=("review_comment:42",),
    )


def _coordinator(
    *,
    state: GuardianState,
    tmp_path: Path,
    broker: _FakeBroker,
    author: _FakeAuthor,
    max_drafts: int = 1,
    api_billed: bool = True,
    max_model_calls_per_day: int = 4,
    model_credential_provider=lambda: "model-key",
    now=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
) -> PreventionCoordinator:
    checkouts = tmp_path / "checkouts"
    checkouts.mkdir()
    return PreventionCoordinator(
        state=state,
        checkout_factory=_FakeCheckoutFactory(
            _base_tree(tmp_path),
            checkouts,
            broker,
        ),
        broker_factory=lambda _policy: broker,
        author=author,
        test_runner=_FakeTestRunner(),
        model_credential_provider=model_credential_provider,
        publish_credential_environment=lambda: {
            "GIT_ASKPASS": "/usr/bin/false",
            "LOCALIZE_GUARDIAN_GIT_TOKEN": "git-token",
        },
        signing_key="signing-key",
        signing_environment=None,
        max_drafts=max_drafts,
        reservation_usd=1.0 if api_billed else None,
        daily_limit_usd=5.0 if api_billed else None,
        max_model_calls_per_day=max_model_calls_per_day,
        api_billed=api_billed,
        temporary_root=tmp_path,
        now=now,
    )


def test_coordinator_authors_proves_signs_publishes_draft_and_deduplicates(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        broker = _FakeBroker()
        author = _FakeAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=broker,
            author=author,
        )
        outcome = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=(_candidate(), _candidate()),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert len(outcome.drafts) == 1
        assert outcome.failures == ()
        assert outcome.skipped == 1
        assert author.calls == 1
        assert broker.open_calls == 1
        assert broker.verify_calls == 2
        assert state.pending_prevention_drafts() == ()
        opened = state.opened_prevention_evidence_hashes(
            source_repository="acme/translations",
            target_repository="guardian/pipeline",
        )
        assert len(opened) == 1

        next_run = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        duplicate = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=(_candidate(),),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=next_run,
            observed_at=now,
            require_live_lease=_live_lease,
        )
        assert duplicate.drafts == ()
        assert duplicate.skipped == 1
        assert author.calls == 1


def test_subscription_prevention_uses_call_cap_without_api_key_or_usd_cost(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        author = _FakeAuthor()

        def forbidden_api_helper() -> str:
            raise AssertionError("subscription prevention must not read an API key")

        outcome = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=author,
            api_billed=False,
            model_credential_provider=forbidden_api_helper,
        ).propose(
            policy=_repository_policy(),
            recurrence_candidates=(_candidate(),),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert len(outcome.drafts) == 1
        assert author.calls == 1
        assert state.model_calls_committed_for_day(now.date()) == 1
        assert state.cost_for_day(now.date()) == 0


def test_coordinator_recovers_pushed_branch_without_new_feedback_or_model_call(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        evidence_hash = "d" * 64
        branch = "guardian/prevention-" + evidence_hash
        ledger = {
            "run_id": run_id,
            "source_repository": "acme/translations",
            "target_repository": "guardian/pipeline",
            "target_base_branch": "main",
            "target_base_sha": BASE_SHA,
            "push_repository": "guardian/pipeline",
            "branch": branch,
            "candidate_sha": CANDIDATE_SHA,
            "evidence_hash": evidence_hash,
            "title": "Prevent recurrence: placeholder parity",
            "body": "Validated body\n",
        }
        state.record_prevention_draft_event(
            **ledger, phase="validated", occurred_at=now
        )
        state.record_prevention_draft_event(**ledger, phase="pushed", occurred_at=now)
        broker = _FakeBroker()
        broker.branch_shas[branch] = CANDIDATE_SHA
        author = _FakeAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=broker,
            author=author,
        )
        outcome = coordinator.recover(
            policy=_repository_policy(),
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert len(outcome.drafts) == 1
        assert author.calls == 0
        assert state.pending_prevention_drafts() == ()


def test_coordinator_recovery_does_not_touch_credentials_without_pending_state(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        broker = _FakeBroker()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=broker,
            author=_FakeAuthor(),
        )

        outcome = coordinator.recover(
            policy=_repository_policy(),
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert outcome == prevention_runtime.PreventionBatchOutcome()
        assert broker.capture_calls == 0


def test_coordinator_defers_over_cap_then_completes_on_next_poll(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        broker = _FakeBroker()
        author = _FakeAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=broker,
            author=author,
            max_drafts=1,
        )
        candidates = (
            _candidate("First distinct pipeline recurrence"),
            _candidate("Second distinct pipeline recurrence"),
        )

        first = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=candidates,
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )
        coordinator.begin_poll()
        second = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=candidates,
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert len(first.drafts) == 1
        assert first.deferred == 1
        assert len(second.drafts) == 1
        assert second.skipped == 1
        assert second.deferred == 0
        assert author.calls == 2
        assert broker.open_calls == 2


def test_failed_candidate_consumes_the_poll_authoring_slot(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    class FirstInvalidAuthor(_FakeAuthor):
        def run(self, *, workspace: Path, **_kwargs) -> PreventionAuthorResult:
            self.calls += 1
            if self.calls == 1:
                (workspace / "localize/rules.py").write_text(
                    "def preserve(value):\n    return value.strip()\n",
                    encoding="utf-8",
                )
                return PreventionAuthorResult(attempts=1, usage=None)
            raise AssertionError("a second candidate must be deferred in this poll")

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        author = FirstInvalidAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=author,
            max_drafts=1,
        )

        outcome = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=(
                _candidate("First invalid recurrence"),
                _candidate("Second recurrence"),
            ),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert author.calls == 1
        assert outcome.drafts == ()
        assert outcome.deferred == 1
        assert outcome.failures == ("PreventionPolicyError",)


def test_open_draft_failures_consume_slot_without_pushing_more_branches(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    class FailingDraftBroker(_FakeBroker):
        def open_draft(self, **kwargs):
            kwargs["before_create"]()
            self.open_calls += 1
            raise PreventionRuntimeError("draft API failed")

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        broker = FailingDraftBroker()
        author = _FakeAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=broker,
            author=author,
            max_drafts=1,
        )
        candidates = (
            _candidate("First pipeline recurrence"),
            _candidate("Second pipeline recurrence"),
        )

        first = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=candidates,
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )

        checkout_factory = coordinator.checkout_factory
        assert isinstance(checkout_factory, _FakeCheckoutFactory)
        assert checkout_factory.publications == 1
        assert author.calls == 1
        assert broker.open_calls == 1
        assert first.deferred == 1
        assert first.failures == ("PreventionRuntimeError",)

        coordinator.begin_poll()
        with pytest.raises(PreventionRuntimeError, match="draft API failed"):
            coordinator.propose(
                policy=_repository_policy(),
                recurrence_candidates=candidates,
                evidence_revision_ids={"review_comment:42": 7},
                run_id=run_id,
                observed_at=now,
                require_live_lease=_live_lease,
            )

        # Recovery retries the one already-pushed branch. It consumes the new
        # poll's sole mutation slot before POST and cannot push another branch.
        assert checkout_factory.publications == 1
        assert author.calls == 1
        assert broker.open_calls == 2


def test_coordinator_treats_model_credential_failure_as_authentication_failure(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    def unavailable() -> str:
        raise RuntimeError("secret helper detail")

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=_FakeAuthor(),
            model_credential_provider=unavailable,
        )

        with pytest.raises(CodexAuthenticationError) as error:
            coordinator.propose(
                policy=_repository_policy(),
                recurrence_candidates=(_candidate(),),
                evidence_revision_ids={"review_comment:42": 7},
                run_id=run_id,
                observed_at=now,
                require_live_lease=_live_lease,
            )

        assert "secret helper detail" not in str(error.value)


def test_coordinator_propagates_github_authentication_failure_to_poll_circuit(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    class AuthenticationFailureBroker(_FakeBroker):
        def verify_publish_authority(self, **_kwargs) -> None:
            raise GitHubAuthenticationError("redacted GitHub authentication failure")

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=AuthenticationFailureBroker(),
            author=_FakeAuthor(),
        )

        with pytest.raises(GitHubAuthenticationError):
            coordinator.propose(
                policy=_repository_policy(),
                recurrence_candidates=(_candidate(),),
                evidence_revision_ids={"review_comment:42": 7},
                run_id=run_id,
                observed_at=now,
                require_live_lease=_live_lease,
            )


def test_coordinator_propagates_model_capacity_failure_to_poll_circuit(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    class CapacityFailureAuthor(_FakeAuthor):
        def run(self, *, workspace: Path, **_kwargs) -> PreventionAuthorResult:
            self.calls += 1
            raise CodexCapacityError("redacted model capacity failure")

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        author = CapacityFailureAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=author,
            api_billed=False,
        )

        with pytest.raises(CodexCapacityError):
            coordinator.propose(
                policy=_repository_policy(),
                recurrence_candidates=(_candidate(),),
                evidence_revision_ids={"review_comment:42": 7},
                run_id=run_id,
                observed_at=now,
                require_live_lease=_live_lease,
            )

        assert author.calls == 1
        assert state.model_calls_committed_for_day(now.date()) == 1


def test_coordinator_accounts_for_each_author_attempt_independently(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    class RetryingAuthor(_FakeAuthor):
        max_attempts = 2

        def run(self, *, workspace: Path, **kwargs) -> PreventionAuthorResult:
            if self.calls == 0:
                self.calls += 1
                raise prevention_runtime.CodexTransientError("first attempt failed")
            return super().run(workspace=workspace, **kwargs)

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        author = RetryingAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=author,
        )

        outcome = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=(_candidate(),),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )

        assert len(outcome.drafts) == 1
        assert author.calls == 2
        # The failed attempt retains its full conservative $1 reservation;
        # only the successful attempt settles to reported usage.
        assert float(state.budget_committed_for_day(now.date())) == pytest.approx(1.01)


def test_coordinator_accounts_author_retries_on_their_actual_utc_days(
    tmp_path: Path,
) -> None:
    first_day = datetime(2026, 8, 30, 23, 59, tzinfo=UTC)
    second_day = datetime(2026, 8, 31, 0, 1, tzinfo=UTC)
    timestamps = iter((first_day, first_day, second_day, second_day))

    class RetryingAuthor(_FakeAuthor):
        max_attempts = 2

        def run(self, *, workspace: Path, **kwargs) -> PreventionAuthorResult:
            if self.calls == 0:
                self.calls += 1
                raise prevention_runtime.CodexTransientError("first attempt failed")
            return super().run(workspace=workspace, **kwargs)

    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=first_day,
        )
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=RetryingAuthor(),
            now=lambda: next(timestamps),
        )

        outcome = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=(_candidate(),),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=first_day,
            require_live_lease=_live_lease,
        )

        assert len(outcome.drafts) == 1
        assert float(state.budget_committed_for_day(first_day.date())) == 1.0
        assert float(state.budget_committed_for_day(second_day.date())) == 0.01


def test_coordinator_skips_project_specific_scope_and_requires_private_opt_in(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        author = _FakeAuthor()
        coordinator = _coordinator(
            state=state,
            tmp_path=tmp_path,
            broker=_FakeBroker(),
            author=author,
        )
        project_candidate = RecurrenceCandidate(
            scope="project_config",
            summary="Change the consuming project's glossary",
            evidence_feedback_ids=("review_comment:42",),
        )
        outcome = coordinator.propose(
            policy=_repository_policy(),
            recurrence_candidates=(project_candidate,),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )
        assert outcome.skipped == 1
        assert author.calls == 0

    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    with GuardianState(private_root / "state.sqlite3") as state:
        run_id = state.start_run(
            repository="acme/translations",
            locale="ru",
            mode=GuardianMode.PROPOSE_PREVENTION,
            started_at=now,
        )
        coordinator = _coordinator(
            state=state,
            tmp_path=private_root,
            broker=_FakeBroker(private=True),
            author=_FakeAuthor(),
        )
        # Source-repository model consent must not authorize a distinct private
        # prevention target.
        with pytest.raises(PreventionRuntimeError, match="opt-in"):
            coordinator.propose(
                policy=_repository_policy(private_opt_in=True),
                recurrence_candidates=(_candidate(),),
                evidence_revision_ids={"review_comment:42": 7},
                run_id=run_id,
                observed_at=now,
                require_live_lease=_live_lease,
            )

        opted_in = coordinator.propose(
            policy=_repository_policy(private_target_opt_in=True),
            recurrence_candidates=(_candidate(),),
            evidence_revision_ids={"review_comment:42": 7},
            run_id=run_id,
            observed_at=now,
            require_live_lease=_live_lease,
        )
        assert len(opted_in.drafts) == 1
