"""Just-in-time, secret-redacting credential helpers for Localize Guardian."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import tempfile

from localize.guardian.process import (
    ProcessLimits,
    ProcessResourceError,
    run_bounded_process,
)

_MAX_SECRET_LENGTH = 8192
_HELPER_ENVIRONMENT_KEYS = frozenset(
    {
        "GH_CONFIG_DIR",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SECURITYSESSIONID",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }
)
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *Password*) printf '%s\\n' "$LOCALIZE_GUARDIAN_GIT_TOKEN" ;;
  *) exit 1 ;;
esac
"""


class CredentialError(RuntimeError):
    """A credential was unavailable or its helper failed safely."""


def _valid_secret(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_SECRET_LENGTH
        and value == value.strip()
        and all(character not in value for character in "\r\n\x00")
    )


def _minimal_helper_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    environment = {
        key: value
        for key, value in values.items()
        if key in _HELPER_ENVIRONMENT_KEYS and value
    }
    environment.setdefault("PATH", os.defpath)
    return environment


@dataclass(frozen=True)
class SecretCommand:
    """Read one credential from an operator-owned argv-only helper."""

    argv: tuple[str, ...]
    timeout_seconds: float = 30.0
    environment: Mapping[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.argv, (str, bytes))
            or not self.argv
            or len(self.argv) > 32
            or any(
                not isinstance(argument, str)
                or not argument
                or len(argument) > 4096
                or not argument.isprintable()
                for argument in self.argv
            )
        ):
            raise ValueError("credential helper argv is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("credential helper timeout must be positive")
        object.__setattr__(self, "argv", tuple(self.argv))

    def read(self) -> str:
        """Invoke the helper once, returning its single-line secret in memory."""

        try:
            completed = run_bounded_process(
                list(self.argv),
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                env=_minimal_helper_environment(self.environment),
                start_new_session=True,
                limits=ProcessLimits.for_timeout(
                    self.timeout_seconds,
                    max_file_size_bytes=16 * 1024,
                ),
            )
        except (OSError, subprocess.SubprocessError, ProcessResourceError):
            raise CredentialError("credential helper could not be executed") from None
        if completed.returncode != 0:
            raise CredentialError("credential helper failed")
        value = completed.stdout.strip()
        if not _valid_secret(value) or completed.stdout.count("\n") > 1:
            raise CredentialError("credential helper returned an invalid credential")
        return value


class CredentialSnapshot:
    """Share one lazy credential-helper issuance for a bounded operation."""

    __slots__ = ("_active", "_command", "_secret")

    def __init__(self, command: SecretCommand) -> None:
        if not isinstance(command, SecretCommand):
            raise TypeError("command must be a SecretCommand")
        self._command = command
        self._secret: str | None = None
        self._active = True

    def read(self) -> str:
        """Return the same in-memory credential for the snapshot lifetime."""

        if not self._active:
            raise CredentialError("credential snapshot is no longer active")
        if self._secret is None:
            self._secret = self._command.read()
        return self._secret

    def close(self) -> None:
        """Drop the snapshot's reference to its credential."""

        self._secret = None
        self._active = False

    def __repr__(self) -> str:
        return "CredentialSnapshot(<redacted>)"


def resolve_model_api_key(command: SecretCommand | None) -> str:
    """Resolve the model credential without persisting or logging it."""

    if command is None:
        raise CredentialError("model credential helper is required")
    return command.read()


@contextmanager
def credential_snapshot(command: SecretCommand) -> Iterator[CredentialSnapshot]:
    """Yield one lazily minted credential and invalidate it on exit."""

    snapshot = CredentialSnapshot(command)
    try:
        yield snapshot
    finally:
        snapshot.close()


@contextmanager
def git_credential_environment(
    command: SecretCommand | CredentialSnapshot,
    *,
    temporary_root: Path | str | None = None,
) -> Iterator[Callable[[], Mapping[str, str]]]:
    """Yield a lazy in-memory token provider for Git's static askpass helper."""

    if not isinstance(command, (SecretCommand, CredentialSnapshot)):
        raise TypeError("command must be a SecretCommand or CredentialSnapshot")
    root = None if temporary_root is None else str(Path(temporary_root))
    with tempfile.TemporaryDirectory(prefix="localize-guardian-credentials-", dir=root) as raw:
        directory = Path(raw)
        directory.chmod(0o700)
        askpass = directory / "git-askpass"
        descriptor = os.open(
            askpass,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o700,
        )
        try:
            os.write(descriptor, _ASKPASS_SCRIPT.encode("utf-8"))
        finally:
            os.close(descriptor)

        cached_secret: str | None = None

        def provide() -> Mapping[str, str]:
            nonlocal cached_secret
            if cached_secret is None:
                cached_secret = command.read()
            return {
                "GIT_ASKPASS": str(askpass),
                "LOCALIZE_GUARDIAN_GIT_TOKEN": cached_secret,
            }

        yield provide


__all__: Sequence[str] = (
    "CredentialError",
    "CredentialSnapshot",
    "SecretCommand",
    "credential_snapshot",
    "git_credential_environment",
    "resolve_model_api_key",
)
