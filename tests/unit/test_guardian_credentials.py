"""Tests for just-in-time Guardian credential brokerage."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from localize.guardian import credentials


def test_secret_command_uses_exact_argv_minimal_environment_and_redacted_errors(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="secret-value\n", stderr="")

    monkeypatch.setattr(credentials, "run_bounded_process", fake_run)
    monkeypatch.setenv("HOME", "/operator/home")
    monkeypatch.setenv("PATH", "/usr/bin:/opt/bin")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak-either")

    command = credentials.SecretCommand(("gh", "auth", "token"), timeout_seconds=7)

    assert command.read() == "secret-value"
    assert observed["argv"] == ["gh", "auth", "token"]
    kwargs = observed["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 7
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["env"]["HOME"] == "/operator/home"
    assert kwargs["env"]["PATH"] == "/usr/bin:/opt/bin"
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "secret-value" not in repr(command)


@pytest.mark.parametrize("stdout", ["", "one\ntwo\n", "x" * 8193, "bad\x00value"])
def test_secret_command_rejects_invalid_output(monkeypatch, stdout: str) -> None:
    monkeypatch.setattr(
        credentials,
        "run_bounded_process",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=stdout, stderr="sensitive diagnostic"
        ),
    )

    with pytest.raises(credentials.CredentialError, match="invalid credential") as exc:
        credentials.SecretCommand(("helper",)).read()

    assert "sensitive" not in str(exc.value)
    if stdout:
        assert stdout not in str(exc.value)


def test_secret_command_failure_never_echoes_helper_output(monkeypatch) -> None:
    monkeypatch.setattr(
        credentials,
        "run_bounded_process",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            1,
            stdout="token-in-stdout",
            stderr="token-in-stderr",
        ),
    )

    with pytest.raises(credentials.CredentialError, match="helper failed") as exc:
        credentials.SecretCommand(("helper",)).read()

    assert "token-in" not in str(exc.value)


def test_model_api_key_requires_helper_and_ignores_ambient_keys(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-env")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env")
    helper = credentials.SecretCommand(("helper",))
    monkeypatch.setattr(credentials.SecretCommand, "read", lambda self: "helper-key")

    assert credentials.resolve_model_api_key(helper) == "helper-key"
    with pytest.raises(credentials.CredentialError, match="helper is required"):
        credentials.resolve_model_api_key(None)


def test_git_credential_provider_keeps_token_out_of_script_and_cleans_up(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = credentials.SecretCommand(("helper",))
    reads = 0

    def read_secret() -> str:
        nonlocal reads
        reads += 1
        return "github-secret"

    monkeypatch.setattr(credentials.SecretCommand, "read", lambda self: read_secret())

    with credentials.git_credential_environment(command, temporary_root=tmp_path) as provider:
        assert reads == 0
        first = provider()
        second = provider()
        helper_path = Path(first["GIT_ASKPASS"])
        assert first == second
        assert reads == 1
        assert helper_path.is_file()
        assert helper_path.stat().st_mode & 0o777 == 0o700
        assert helper_path.parent.stat().st_mode & 0o777 == 0o700
        helper_text = helper_path.read_text(encoding="utf-8")
        assert "github-secret" not in helper_text
        assert first["LOCALIZE_GUARDIAN_GIT_TOKEN"] == "github-secret"
        assert os.access(helper_path, os.X_OK)

    assert not helper_path.exists()
    assert not helper_path.parent.exists()


def test_runtime_command_arguments_reject_control_characters() -> None:
    with pytest.raises(ValueError, match="argv"):
        credentials.SecretCommand(("helper", "bad\nargument"))
    with pytest.raises(ValueError, match="timeout"):
        credentials.SecretCommand(("helper",), timeout_seconds=0)
