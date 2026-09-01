"""Behavioral tests for the self-hosted Guardian command surface."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from localize import cli as root_cli
from localize.guardian import FeedbackEvent, GuardianMode
from localize.guardian import cli
from localize.guardian.config import load_guardian_config
from localize.guardian.github import GitHubRepositoryIdentity
from localize.guardian.state import GuardianState


UTC = timezone.utc
_REAL_CODEX_CAPABILITY_PROBE = cli._codex_capability_probe
_REAL_CODEX_CHATGPT_LOGIN_READY = cli._codex_chatgpt_login_ready


@pytest.fixture(autouse=True)
def _successful_codex_capability_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_codex_capability_probe",
        lambda _executable, **_kwargs: True,
    )
    monkeypatch.setattr(
        cli,
        "_codex_chatgpt_login_ready",
        lambda _config: True,
    )


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "operator" / "guardian.yaml"
    assert cli.main(["init", "--config", str(config_path)]) == 0
    return config_path


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _replace_once(value: str, needle: str, replacement: str) -> str:
    assert needle in value, f"template drift for {needle!r}"
    return value.replace(needle, replacement, 1)


def _configure_scheduled_runtime(
    config_path: Path,
    root: Path,
    *,
    api_key: bool = True,
) -> tuple[Path, Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    codex = bin_dir / "codex"
    github_helper = bin_dir / "github-token"
    model_helper = bin_dir / "model-token"
    git = bin_dir / "git"
    gpg = bin_dir / "gpg"
    for executable in (codex, github_helper, model_helper, git, gpg):
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
    config = config_path.read_text(encoding="utf-8")
    config = _replace_once(
        config,
        "  codex_executable: codex",
        f"  codex_executable: {codex}",
    )
    config = _replace_once(config, "  git_executable: git", f"  git_executable: {git}")
    config = _replace_once(config, "  signing_program: gpg", f"  signing_program: {gpg}")
    config = _replace_once(
        config,
        "  github_token_command: [gh, auth, token]",
        f"  github_token_command: [{github_helper}]",
    )
    if api_key:
        config = _replace_once(
            config,
            "  codex_auth_mode: chatgpt",
            "  codex_auth_mode: api-key",
        )
        config = _replace_once(
            config,
            "  codex_home: ~/.local/share/localize-guardian/codex\n",
            "",
        )
        config = _replace_once(
            config,
            "  # codex_api_key_command:",
            f"  codex_api_key_command: [{model_helper}]\n  # codex_api_key_command:",
        )
        config = _replace_once(
            config,
            "  # daily_cost_limit_usd: 2.00",
            "  daily_cost_limit_usd: 2.00",
        )
        config = _replace_once(
            config,
            "  # model_call_reservation_usd: 2.00",
            "  model_call_reservation_usd: 2.00",
        )
    config_path.write_text(config, encoding="utf-8")
    return codex, github_helper, model_helper


def test_init_creates_valid_report_only_config_and_private_runtime_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "operator" / "guardian.yaml"

    exit_code = cli.main(["init", "--config", str(config_path)])

    captured = capsys.readouterr()
    config = load_guardian_config(config_path)
    assert exit_code == 0
    assert config.mode is GuardianMode.OBSERVE
    assert config.report_only
    assert config.repositories[0].base_repo == "acme/widgets"
    assert _mode(config_path) == 0o600
    assert _mode(config_path.parent / ".guardian") == 0o700
    assert "Created report-only Guardian config" in captured.out
    assert "OPENAI_API_KEY" not in config_path.read_text(encoding="utf-8")


def test_init_refuses_to_overwrite_existing_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "guardian.yaml"
    config_path.write_text("owner: operator\n", encoding="utf-8")

    exit_code = cli.main(["init", "--config", str(config_path)])

    assert exit_code == 1
    assert config_path.read_text(encoding="utf-8") == "owner: operator\n"
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize(
    "message",
    (
        "Guardian configuration is unavailable or unsafe.",
        "Guardian configuration is invalid.",
    ),
)
def test_config_loader_preserves_redacted_failure_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    def fail(_path: Path):
        raise cli.GuardianRuntimeError(message)

    monkeypatch.setattr(cli, "load_trusted_guardian_config", fail)

    with pytest.raises(cli.GuardianCLIError, match=message.replace(".", r"\.")):
        cli._load_config_or_raise(tmp_path / "guardian.yaml")


def test_login_uses_dedicated_chatgpt_home_and_never_inherits_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    codex_home = tmp_path / "private-codex-home"
    config_path.write_text(
        _replace_once(
            config_path.read_text(encoding="utf-8"),
            "~/.local/share/localize-guardian/codex",
            str(codex_home),
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    def fake_login(argv, **kwargs):
        observed["argv"] = list(argv)
        observed["env"] = dict(kwargs["env"])
        auth_file = codex_home / "auth.json"
        auth_file.write_text('{"auth":"test-only"}', encoding="utf-8")
        auth_file.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli, "_command_available", lambda _command: True)
    monkeypatch.setattr(cli.subprocess, "run", fake_login)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-cross-either")

    exit_code = cli.main(["login", "--config", str(config_path)])

    assert exit_code == 0
    assert _mode(codex_home) == 0o700
    assert _mode(codex_home / "auth.json") == 0o600
    assert "--device-auth" in observed["argv"]
    assert 'forced_login_method="chatgpt"' in observed["argv"]
    assert observed["env"]["CODEX_HOME"] == str(codex_home)
    assert "OPENAI_API_KEY" not in observed["env"]
    assert "CODEX_API_KEY" not in observed["env"]
    assert "subscription login ready" in capsys.readouterr().out


def test_chatgpt_login_status_is_checked_without_a_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"auth":"test-only"}', encoding="utf-8")
    auth_file.chmod(0o600)
    config_path.write_text(
        _replace_once(
            config_path.read_text(encoding="utf-8"),
            "~/.local/share/localize-guardian/codex",
            str(codex_home),
        ),
        encoding="utf-8",
    )
    config = load_guardian_config(config_path)
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = list(argv)
        observed["env"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Logged in using ChatGPT\n",
            stderr="",
        )

    monkeypatch.setattr(cli, "run_bounded_process", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")

    assert _REAL_CODEX_CHATGPT_LOGIN_READY(config) is True
    assert "exec" not in observed["argv"]
    assert observed["argv"][-1] == "status"
    assert "OPENAI_API_KEY" not in observed["env"]


def test_login_rejects_writable_codex_home_ancestor_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    codex_home = unsafe_parent / "codex-home"
    config_path.write_text(
        _replace_once(
            config_path.read_text(encoding="utf-8"),
            "~/.local/share/localize-guardian/codex",
            str(codex_home),
        ),
        encoding="utf-8",
    )
    attempted = False

    def unexpected_login(*_args, **_kwargs):
        nonlocal attempted
        attempted = True
        raise AssertionError("Codex login must not run under an unsafe ancestor")

    monkeypatch.setattr(cli, "_command_available", lambda _command: True)
    monkeypatch.setattr(cli.subprocess, "run", unexpected_login)

    exit_code = cli.main(["login", "--config", str(config_path)])

    assert exit_code == 1
    assert attempted is False
    assert not codex_home.exists()
    assert "unsafe" in capsys.readouterr().err.casefold()


@pytest.mark.parametrize("operation", ["fchmod", "fsync"])
def test_exclusive_write_removes_its_partial_inode_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    destination = tmp_path / "guardian-file"

    def fail(*_args, **_kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(cli.os, operation, fail)

    with pytest.raises(cli.GuardianCLIError, match="complete Guardian file"):
        cli._write_exclusive(destination, "content\n", mode=0o600)

    assert not destination.exists()


def test_exclusive_write_never_unlinks_a_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "guardian-file"

    def replace_then_fail(_descriptor: int) -> None:
        destination.unlink()
        destination.write_text("replacement\n", encoding="utf-8")
        raise OSError("injected fsync failure")

    monkeypatch.setattr(cli.os, "fsync", replace_then_fail)

    with pytest.raises(cli.GuardianCLIError, match="complete Guardian file"):
        cli._write_exclusive(destination, "partial\n", mode=0o600)

    assert destination.read_text(encoding="utf-8") == "replacement\n"


def test_doctor_validates_local_dependencies_and_exact_github_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-only-test-key")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    github_probe = Mock(
        return_value=(
            GitHubRepositoryIdentity(
                full_name="acme/widgets",
                repository_id=100000001,
                private=False,
            ),
        )
    )
    monkeypatch.setattr(cli, "_probe_github", github_probe)
    monkeypatch.setattr(
        cli, "_signing_key_configured", lambda _configured, **_kwargs: False
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Guardian doctor" in captured.out
    assert "config: ok (observe)" in captured.out
    assert "state directory: ok" in captured.out
    assert "Codex executable: ok" in captured.out
    assert "Codex capability canary: ok" in captured.out
    assert "result schema: ok" in captured.out
    assert "GitHub credential helper: ok" in captured.out
    assert "repository acme/widgets: ok (public, id=100000001)" in captured.out
    assert "commit signing: not required (observe mode)" in captured.out
    github_probe.assert_called_once()


def test_doctor_fails_when_codex_permission_canary_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-only-test-key")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(
        cli,
        "_codex_capability_probe",
        lambda _executable, **_kwargs: False,
    )
    monkeypatch.setattr(
        cli,
        "_probe_github",
        lambda _config: (
            GitHubRepositoryIdentity("acme/widgets", 100000001, False),
        ),
    )
    monkeypatch.setattr(
        cli, "_signing_key_configured", lambda _configured, **_kwargs: False
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    assert exit_code == 1
    assert "Codex capability canary: error" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("authoring", "profile", "filesystem_setting", "write_flag"),
    [
        (
            False,
            "guardian_evidence",
            'permissions.guardian_evidence.filesystem={":minimal"="read",'
            '":workspace_roots"={"."="read"}}',
            "0",
        ),
        (
            True,
            "guardian_prevention_author",
            'permissions.guardian_prevention_author.filesystem={":minimal"="read",'
            '":workspace_roots"={"."="write"}}',
            "1",
        ),
    ],
)
def test_codex_capability_probe_uses_exact_isolated_profile_and_flags(
    monkeypatch: pytest.MonkeyPatch,
    authoring: bool,
    profile: str,
    filesystem_setting: str,
    write_flag: str,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    @contextmanager
    def fake_canaries():
        yield 54321, "/private/tmp/guardian-canary.sock"

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_doctor_network_canaries", fake_canaries)
    monkeypatch.setattr(cli, "run_bounded_process", fake_run)
    cgroup_parent_procs = Path("/sys/fs/cgroup/guardian/cgroup.procs")
    monkeypatch.setattr(
        cli,
        "linux_cgroup_parent_procs",
        lambda: cgroup_parent_procs,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-cross")

    assert _REAL_CODEX_CAPABILITY_PROBE("/trusted/codex", authoring=authoring)

    assert len(calls) == 2
    flag_argv, flag_kwargs = calls[0]
    sandbox_argv, sandbox_kwargs = calls[1]
    assert flag_argv[1:3] == ["--ask-for-approval", "never"]
    assert flag_argv[-6:] == [
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--help",
    ]
    assert filesystem_setting in flag_argv
    assert sandbox_argv[sandbox_argv.index("--permission-profile") + 1] == profile
    assert filesystem_setting in sandbox_argv
    assert sandbox_argv[-2:] == [write_flag, str(cgroup_parent_procs)]
    assert sandbox_argv[sandbox_argv.index("--") + 1 :][:2] == ["/bin/sh", "-c"]
    for kwargs in (flag_kwargs, sandbox_kwargs):
        assert kwargs["shell"] is False
        assert kwargs["limits"].require_linux_cgroup is True
        environment = kwargs["env"]
        assert "OPENAI_API_KEY" not in environment
        assert "GITHUB_TOKEN" not in environment
        assert set(environment) == {"CODEX_HOME", "HOME", "NO_COLOR", "PATH", "TMPDIR"}


def test_codex_capability_probe_fails_when_confinement_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def fake_canaries():
        yield 54321, ""

    calls = 0

    def fake_run(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv,
            0 if calls == 1 else 1,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(cli, "_doctor_network_canaries", fake_canaries)
    monkeypatch.setattr(cli, "run_bounded_process", fake_run)

    assert _REAL_CODEX_CAPABILITY_PROBE("/trusted/codex") is False


def test_doctor_github_probe_disables_environment_proxy_inheritance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    config = load_guardian_config(config_path)
    monkeypatch.setattr(cli.SecretCommand, "read", lambda _self: "test-token")
    reader = Mock()
    reader.repository_identity.return_value = GitHubRepositoryIdentity(
        "acme/widgets",
        100000001,
        False,
    )
    monkeypatch.setattr(cli, "GitHubReader", lambda _client, _policy: reader)

    with patch("localize.guardian.cli.httpx.Client") as client_factory:
        client_factory.return_value.__enter__.return_value = object()
        identities = cli._probe_github(config)

    assert identities[0].repository_id == 100000001
    assert client_factory.call_args.kwargs["trust_env"] is False


def test_doctor_never_prints_helper_or_environment_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    secret = "guardian-super-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(
        cli,
        "_probe_github",
        Mock(side_effect=RuntimeError(f"token was {secret}")),
    )
    monkeypatch.setattr(
        cli, "_signing_key_configured", lambda _configured, **_kwargs: False
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    output = capsys.readouterr()
    assert exit_code == 1
    assert secret not in output.out
    assert secret not in output.err
    assert "GitHub read-only probe: error" in output.out


def test_doctor_requires_signing_only_for_write_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-only-test-key")

    def command_available(command: tuple[str, ...]) -> bool:
        return command != ("gpg",)

    monkeypatch.setattr(cli, "_command_available", command_available)
    monkeypatch.setattr(
        cli,
        "_probe_github",
        lambda _config: (GitHubRepositoryIdentity("acme/widgets", 100000001, False),),
    )
    signing_probe = Mock(return_value=False)
    monkeypatch.setattr(cli, "_signing_key_configured", signing_probe)

    observe_exit = cli.main(["doctor", "--config", str(config_path)])
    observe_output = capsys.readouterr().out
    config_path.write_text(
        _replace_once(
            config_path.read_text(encoding="utf-8"),
            "mode: observe",
            "mode: apply-owned-translations",
        ),
        encoding="utf-8",
    )
    apply_exit = cli.main(["doctor", "--config", str(config_path)])
    apply_output = capsys.readouterr().out

    assert observe_exit == 0
    assert "Signing program: not required (observe mode)" in observe_output
    assert "commit signing: not required (observe mode)" in observe_output
    signing_probe.assert_not_called()
    assert apply_exit == 1
    assert "Signing program: error (not found)" in apply_output
    assert "commit signing: error" in apply_output


def test_signing_probe_never_falls_back_to_global_git_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=["git", "config", "--get", "user.signingkey"],
            returncode=0,
            stdout="GLOBAL-KEY\n",
            stderr="",
        )
    )
    monkeypatch.setattr(cli, "run_bounded_process", run)

    assert (
        cli._signing_key_configured(
            None,
            git_executable="/usr/bin/git",
            signing_program="/usr/bin/gpg",
        )
        is False
    )
    run.assert_not_called()


def test_signing_probe_signs_and_verifies_with_exact_key_in_isolated_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir()
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_command_available", lambda _command: True)
    calls: list[tuple[list[str], dict[str, str]]] = []

    fingerprint = "A" * 40

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, dict(kwargs["env"])))  # type: ignore[arg-type]
        stderr = f"[GNUPG:] VALIDSIG {fingerprint}\n" if "verify-commit" in argv else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)

    monkeypatch.setattr(cli, "run_bounded_process", run)

    assert cli._signing_key_configured(
        fingerprint,
        git_executable="/usr/bin/git",
        signing_program="/usr/bin/gpg",
    ) is True
    assert len(calls) == 3
    assert calls[0][0][0:3] == ["/usr/bin/git", "-c", "gpg.program=/usr/bin/gpg"]
    assert f"-S{fingerprint}" in calls[1][0]
    assert calls[2][0][-3:] == ["verify-commit", "--raw", "HEAD"]
    for _argv, environment in calls:
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GNUPGHOME"] == str(gnupg_home.resolve())
        assert "OPENAI_API_KEY" not in environment


def test_signing_probe_fails_closed_when_exact_key_cannot_sign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir()
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(cli, "_command_available", lambda _command: True)
    calls = 0

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0 if calls == 1 else 1, "", "")

    monkeypatch.setattr(cli, "run_bounded_process", run)

    assert cli._signing_key_configured(
        "B" * 40,
        git_executable="/usr/bin/git",
        signing_program="/usr/bin/gpg",
    ) is False
    assert calls == 2


def test_doctor_consumes_secret_free_runtime_commands_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    codex = tmp_path / "bin" / "codex-custom"
    github_helper = tmp_path / "bin" / "github-token"
    api_helper = tmp_path / "bin" / "model-token"
    codex.parent.mkdir()
    for executable in (codex, github_helper, api_helper):
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
    generated = config_path.read_text(encoding="utf-8")
    config_path.write_text(
            "runtime:\n"
            "  codex_auth_mode: api-key\n"
        "  codex_model: gpt-5.6-terra\n"
        "  codex_reasoning_effort: high\n"
        f"  codex_executable: {codex}\n"
        "  git_executable: /usr/bin/git\n"
        "  signing_program: /usr/bin/gpg\n"
        f"  github_token_command: [{github_helper}]\n"
        f"  codex_api_key_command: [{api_helper}]\n"
            f"  signing_key: {'A' * 40}\n"
            + "limits:\n"
            + "  daily_cost_limit_usd: 2.00\n"
            + "  model_call_reservation_usd: 2.00\n"
            + generated.split("limits:\n", 1)[1],
        encoding="utf-8",
    )
    # The expression above preserves the generated limits/repository policy while
    # replacing only its runtime block.
    config = load_guardian_config(config_path)
    github_probe = Mock(
        return_value=(GitHubRepositoryIdentity("acme/widgets", 100000001, False),)
    )
    monkeypatch.setattr(cli, "_probe_github", github_probe)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        cli, "_credential_helper_works", lambda command: command == (str(api_helper),)
    )
    signing_probe = Mock(return_value=True)
    monkeypatch.setattr(cli, "_signing_key_configured", signing_probe)

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Codex model: gpt-5.6-terra (high)" in output
    github_probe.assert_called_once_with(config)
    assert "Signing program: not required (observe mode)" in output
    signing_probe.assert_not_called()


def test_doctor_does_not_create_a_missing_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    state_dir = cli.guardian_state_dir(config_path)
    state_dir.rmdir()
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-only-test-key")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cli,
        "_probe_github",
        lambda _config: (GitHubRepositoryIdentity("acme/widgets", 100000001, False),),
    )
    monkeypatch.setattr(
        cli, "_signing_key_configured", lambda _configured, **_kwargs: False
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    assert exit_code == 0
    assert not state_dir.exists()
    assert "created on first run or install" in capsys.readouterr().out


def test_run_creates_private_state_file_and_lazily_calls_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    run_once = Mock(return_value=None)
    imported = Mock(return_value=SimpleNamespace(run_once=run_once))
    monkeypatch.setattr(cli.importlib, "import_module", imported)

    exit_code = cli.main(["run", "--config", str(config_path), "--scheduled"])

    state_path = cli.guardian_state_path(config_path)
    assert exit_code == 0
    assert state_path.is_file()
    assert _mode(state_path) == 0o600
    imported.assert_called_once_with("localize.guardian.controller")
    run_once.assert_called_once_with(config_path=config_path.resolve(), scheduled=True)


def test_run_reports_controller_failure_without_echoing_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    secret = "comment-or-token-secret"
    runtime = SimpleNamespace(run_once=Mock(side_effect=RuntimeError(secret)))
    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: runtime)

    exit_code = cli.main(["run", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.out
    assert secret not in captured.err
    assert "RuntimeError" in captured.err


def test_run_refuses_an_insecure_existing_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    cli.guardian_state_dir(config_path).chmod(0o755)
    imported = Mock()
    monkeypatch.setattr(cli.importlib, "import_module", imported)

    exit_code = cli.main(["run", "--config", str(config_path)])

    assert exit_code == 1
    assert _mode(cli.guardian_state_dir(config_path)) == 0o755
    imported.assert_not_called()
    assert "GuardianCLIError" in capsys.readouterr().err


def test_run_refuses_a_group_writable_authority_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    config_path.chmod(0o620)
    imported = Mock()
    monkeypatch.setattr(cli.importlib, "import_module", imported)

    exit_code = cli.main(["run", "--config", str(config_path)])

    assert exit_code == 1
    imported.assert_not_called()
    assert "GuardianCLIError" in capsys.readouterr().err


def test_status_summarizes_audit_metadata_without_raw_bodies_or_messages(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    state_path = cli.guardian_state_path(config_path)
    secret = "review body with guardian-super-secret"
    with GuardianState(state_path) as state:
        revision = state.record_feedback_event(
            FeedbackEvent(
                repository="acme/widgets",
                pr_number=17,
                kind="review-comment",
                event_id="91",
                author="reviewer",
                author_id=100000004,
                author_type="User",
                body=secret,
                head_sha="a" * 40,
                base_sha="b" * 40,
                locale="de",
            )
        )
        run_id = state.start_run(
            repository="acme/widgets",
            locale="de",
            mode=GuardianMode.OBSERVE,
            started_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
        )
        state.record_action(
            run_id=run_id,
            event_revision_id=revision.revision_id,
            action="observe",
            status="completed",
        )
        state.finish_run(
            run_id,
            status="completed",
            summary=f"unsafe summary {secret}",
            finished_at=datetime(2026, 8, 30, 9, 1, tzinfo=UTC),
        )
        state.record_health(
            component="github",
            status="ok",
            message=f"unsafe health detail {secret}",
        )
    os.chmod(state_path, 0o600)

    exit_code = cli.main(["status", "--config", str(config_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Guardian status" in output
    assert "mode: observe" in output
    assert "last completed run: 2026-08-30T09:01:00" in output
    assert "pending feedback revisions: 0" in output
    assert "actions: completed=1" in output
    assert "health: github=ok" in output
    assert secret not in output


def test_status_is_read_only_when_no_state_database_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    state_path = cli.guardian_state_path(config_path)
    assert not state_path.exists()

    exit_code = cli.main(["status", "--config", str(config_path)])

    assert exit_code == 0
    assert not state_path.exists()
    assert "state: no runs recorded" in capsys.readouterr().out


def test_status_refuses_a_symlinked_state_database_without_reading_its_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    secret = "state-target-secret"
    target = tmp_path / "foreign-state"
    target.write_text(secret, encoding="utf-8")
    cli.guardian_state_path(config_path).symlink_to(target)

    exit_code = cli.main(["status", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.out
    assert secret not in captured.err
    assert "unavailable or invalid" in captured.err


def test_install_stages_secret_free_launchd_artifacts_without_loading_or_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    executable = tmp_path / "bin" / "localize"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    _configure_scheduled_runtime(config_path, tmp_path)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    monkeypatch.setattr(cli, "_default_launch_agents_dir", lambda: launch_agents)
    secret = "must-not-be-embedded"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    first_exit = cli.main(
        [
            "install",
            "--config",
            str(config_path),
            "--executable",
            str(executable),
        ]
    )
    first_output = capsys.readouterr()

    paths = cli.guardian_install_paths(config_path)
    assert first_exit == 0
    assert paths.runner_path.is_file()
    assert paths.plist_path.is_file()
    assert paths.stdout_path.is_file()
    assert paths.stderr_path.is_file()
    assert _mode(paths.runner_path) == 0o700
    assert _mode(paths.plist_path) == 0o600
    assert _mode(paths.stdout_path) == 0o600
    assert _mode(paths.stderr_path) == 0o600
    combined = paths.runner_path.read_text() + paths.plist_path.read_text()
    assert str(config_path.resolve()) in combined
    assert secret not in combined
    assert "launchctl" not in combined
    assert "staged but not loaded" in first_output.out

    original_runner = paths.runner_path.read_bytes()
    second_exit = cli.main(
        [
            "install",
            "--config",
            str(config_path),
            "--executable",
            str(executable),
        ]
    )
    assert second_exit == 1
    assert paths.runner_path.read_bytes() == original_runner
    assert "already exists" in capsys.readouterr().err


def test_install_subscription_mode_requires_no_api_credential_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    _configure_scheduled_runtime(config_path, tmp_path, api_key=False)
    config_path.write_text(
        _replace_once(
            config_path.read_text(encoding="utf-8"),
            f"  signing_program: {tmp_path / 'bin' / 'gpg'}",
            "  signing_program: gpg",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 0
    assert "codex_api_key_command:" not in "\n".join(
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert cli.guardian_install_paths(config_path).plist_path.exists()


def test_install_write_mode_requires_absolute_signing_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    _configure_scheduled_runtime(config_path, tmp_path, api_key=False)
    configured = config_path.read_text(encoding="utf-8")
    configured = _replace_once(
        configured,
        "mode: observe",
        "mode: apply-owned-translations",
    )
    configured = _replace_once(
        configured,
        f"  signing_program: {tmp_path / 'bin' / 'gpg'}",
        "  signing_program: gpg",
    )
    config_path.write_text(
        configured,
        encoding="utf-8",
    )
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert "runtime.signing_program" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "replacement", "expected_error"),
    [
        ("codex", "codex", "codex_executable"),
        ("git", "git", "git_executable"),
        ("github", "gh", "github_token_command"),
        ("model", "model-token", "codex_api_key_command"),
    ],
)
def test_install_requires_absolute_executables_for_unattended_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    replacement: str,
    expected_error: str,
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    codex, github_helper, model_helper = _configure_scheduled_runtime(
        config_path, tmp_path
    )
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    configured = config_path.read_text(encoding="utf-8")
    absolute_value = {
        "codex": str(codex),
        "git": str(tmp_path / "bin" / "git"),
        "signing": str(tmp_path / "bin" / "gpg"),
        "github": str(github_helper),
        "model": str(model_helper),
    }[field]
    config_path.write_text(
        _replace_once(configured, absolute_value, replacement),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert expected_error in capsys.readouterr().err
    paths = cli.guardian_install_paths(config_path)
    assert not paths.runner_path.exists()
    assert not paths.plist_path.exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "group-writable"])
def test_install_rejects_mutable_or_redirected_scheduled_executables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unsafe_kind: str,
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    codex, _github_helper, _model_helper = _configure_scheduled_runtime(
        config_path, tmp_path
    )
    if unsafe_kind == "symlink":
        target = codex.with_name("codex-target")
        codex.rename(target)
        codex.symlink_to(target)
    else:
        codex.chmod(0o720)
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert "runtime.codex_executable" in capsys.readouterr().err
    paths = cli.guardian_install_paths(config_path)
    assert not paths.runner_path.exists()
    assert not paths.plist_path.exists()


def test_install_rejects_group_writable_localize_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    _configure_scheduled_runtime(config_path, tmp_path)
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o720)
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert "Localize executable" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["codex", "localize"])
def test_install_rejects_path_dependent_env_shebangs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    codex, _github_helper, _model_helper = _configure_scheduled_runtime(
        config_path, tmp_path
    )
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    target = codex if field == "codex" else executable
    target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    target.chmod(0o700)
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    expected = "runtime.codex_executable" if field == "codex" else "Localize executable"
    assert expected in capsys.readouterr().err


def test_install_rolls_back_only_files_created_by_a_failed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    _configure_scheduled_runtime(config_path, tmp_path)
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: tmp_path / "Library" / "LaunchAgents",
    )
    paths = cli.guardian_install_paths(config_path)
    paths.stdout_path.write_text("keep this log\n", encoding="utf-8")
    paths.stdout_path.chmod(0o600)
    original_write = cli._write_exclusive

    def fail_plist(path: Path, content: str, *, mode: int) -> tuple[int, int]:
        if path == paths.plist_path:
            raise cli.GuardianCLIError("simulated plist failure")
        return original_write(path, content, mode=mode)

    monkeypatch.setattr(cli, "_write_exclusive", fail_plist)

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert "simulated plist failure" in capsys.readouterr().err
    assert not paths.runner_path.exists()
    assert not paths.plist_path.exists()
    assert paths.stdout_path.read_text(encoding="utf-8") == "keep this log\n"
    assert not paths.stderr_path.exists()


def test_install_refuses_a_symlinked_launch_agents_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    _configure_scheduled_runtime(config_path, tmp_path)
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    library = tmp_path / "Library"
    library.mkdir()
    target = tmp_path / "redirected-launch-agents"
    target.mkdir()
    launch_agents = library / "LaunchAgents"
    launch_agents.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(cli, "_default_launch_agents_dir", lambda: launch_agents)

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert "symlinked" in capsys.readouterr().err
    assert tuple(target.iterdir()) == ()


def test_install_refuses_a_symlinked_launch_agents_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _init_config(tmp_path)
    capsys.readouterr()
    _configure_scheduled_runtime(config_path, tmp_path)
    executable = tmp_path / "localize"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    redirected = tmp_path / "redirected-library"
    redirected.mkdir()
    library = tmp_path / "Library"
    library.symlink_to(redirected, target_is_directory=True)
    monkeypatch.setattr(
        cli,
        "_default_launch_agents_dir",
        lambda: library / "LaunchAgents",
    )

    exit_code = cli.main(
        ["install", "--config", str(config_path), "--executable", str(executable)]
    )

    assert exit_code == 1
    assert "ancestor" in capsys.readouterr().err
    assert tuple(redirected.iterdir()) == ()


def test_root_cli_delegates_guardian_arguments_through_a_lazy_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardian_main = Mock(return_value=7)
    original_import = root_cli.importlib.import_module

    def import_module(name: str):
        if name == "localize.guardian.cli":
            return SimpleNamespace(main=guardian_main)
        return original_import(name)

    monkeypatch.setattr(root_cli.importlib, "import_module", import_module)

    exit_code = root_cli.main(["guardian", "doctor", "--config", "/tmp/policy.yaml"])

    assert exit_code == 7
    guardian_main.assert_called_once_with(["doctor", "--config", "/tmp/policy.yaml"])


def test_root_cli_never_loads_translation_plugins_for_guardian(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardian_main = Mock(return_value=0)
    plugin_loader = Mock()
    monkeypatch.setenv("LOCALIZE_PLUGIN_MODULES", "untrusted.translation_plugin")
    original_import = root_cli.importlib.import_module

    def import_module(name: str):
        if name == "localize.guardian.cli":
            return SimpleNamespace(main=guardian_main)
        return original_import(name)

    monkeypatch.setattr(root_cli.importlib, "import_module", import_module)
    monkeypatch.setattr(root_cli, "load_plugins", plugin_loader)

    assert root_cli.main(
        ["guardian", "status", "--config", "/tmp/policy.yaml"]
    ) == 0
    plugin_loader.assert_not_called()

    with pytest.raises(SystemExit):
        root_cli.main(
            [
                "--plugin",
                "untrusted.translation_plugin",
                "guardian",
                "status",
                "--config",
                "/tmp/policy.yaml",
            ]
        )
    plugin_loader.assert_not_called()


def test_guardian_result_schema_is_declared_as_wheel_package_data() -> None:
    import tomllib

    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["tool"]["setuptools"]["package-data"]["localize.guardian"] == [
        "schemas/*.json"
    ]
    schema = project_root / "localize/guardian/schemas/guardian-result.schema.json"
    assert json.loads(schema.read_text(encoding="utf-8"))["type"] == "object"
