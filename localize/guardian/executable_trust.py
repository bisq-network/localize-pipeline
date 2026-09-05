"""Shared executable-integrity checks for unattended Guardian processes."""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import re
import stat

from localize.guardian.filesystem_trust import is_trusted_directory


class ExecutableTrustError(ValueError):
    """A configured executable or interpreter chain is mutable or ambiguous."""


_INDIRECT_COMMAND_LAUNCHERS = frozenset(
    {
        "busybox",
        "chrt",
        "command",
        "deno",
        "doas",
        "env",
        "expect",
        "ionice",
        "nice",
        "nohup",
        "npm",
        "npx",
        "open",
        "osascript",
        "pnpm",
        "script",
        "setsid",
        "stdbuf",
        "sudo",
        "time",
        "timeout",
        "xargs",
        "yarn",
    }
)
_VERSIONED_INTERPRETER_RE = re.compile(
    r"^(?:bash|bun|dash|fish|java|javaw|ksh|lua|node|nodejs|perl|php|"
    r"pypy|python|pythonw|ruby|sh|zsh)(?:\d+(?:\.\d+)*)?(?:\.exe)?$",
    re.IGNORECASE,
)


def is_indirect_command_launcher(executable: str) -> bool:
    """Return whether argv[0] delegates execution to an unchecked argument."""

    name = Path(executable).name.casefold()
    return name in _INDIRECT_COMMAND_LAUNCHERS or bool(
        _VERSIONED_INTERPRETER_RE.fullmatch(name)
    )


def is_supported_direct_helper_command(
    command: Sequence[str],
    *,
    allow_github_cli: bool = False,
) -> bool:
    """Return whether argv names one dedicated helper or exact ``gh auth token``."""

    if len(command) == 1:
        return not is_indirect_command_launcher(command[0])
    return bool(
        allow_github_cli
        and len(command) == 3
        and Path(command[0]).name.casefold() == "gh"
        and tuple(command[1:]) == ("auth", "token")
    )


def _trusted_owners() -> frozenset[int]:
    owners = {0}
    if hasattr(os, "getuid"):
        owners.add(os.getuid())
    return frozenset(owners)


def require_absolute_trusted_executable(
    command: Sequence[str],
    *,
    field: str,
) -> None:
    """Require an absolute, owner-controlled executable and interpreter chain."""

    if not command:
        raise ExecutableTrustError(f"{field} must name an executable.")
    executable = Path(command[0]).expanduser()
    if not executable.is_absolute():
        raise ExecutableTrustError(
            f"{field} must use an absolute executable path for scheduled runs."
        )
    _require_trusted_executable(executable, field=field, seen=frozenset())


def require_absolute_trusted_direct_executable(
    command: Sequence[str],
    *,
    field: str,
    allow_github_cli: bool = False,
) -> None:
    """Require one inspected helper executable or the exact GitHub CLI helper."""

    require_absolute_trusted_executable(command, field=field)
    if not is_supported_direct_helper_command(
        command,
        allow_github_cli=allow_github_cli,
    ):
        raise ExecutableTrustError(
            f"{field} must invoke one dedicated trusted helper executable directly; "
            "arguments are not accepted, and the executable must not be an "
            "interpreter or command dispatcher."
        )


def require_absolute_trusted_wrapper(
    command: Sequence[str],
    *,
    field: str,
) -> None:
    """Require a one-element argv naming one directly inspected wrapper."""

    if len(command) != 1:
        raise ExecutableTrustError(
            f"{field} must contain exactly one direct wrapper executable; "
            "policy and script arguments are not accepted."
        )
    require_absolute_trusted_direct_executable(command, field=field)


def _require_trusted_executable(
    executable: Path,
    *,
    field: str,
    seen: frozenset[Path],
) -> None:
    if executable in seen:
        raise ExecutableTrustError(f"{field} has a recursive interpreter chain.")
    try:
        metadata = executable.stat(follow_symlinks=False)
    except OSError:
        raise ExecutableTrustError(
            f"{field} executable was not found or is not executable."
        ) from None
    trusted_owners = _trusted_owners()
    if (
        executable.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in trusted_owners
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise ExecutableTrustError(
            f"{field} must be a trusted, non-symlinked executable that is not "
            "writable by group or other users."
        )

    ancestor = executable.parent
    while True:
        try:
            ancestor_metadata = ancestor.stat(follow_symlinks=False)
        except OSError:
            raise ExecutableTrustError(
                f"{field} executable has an unsafe ancestor path."
            ) from None
        if ancestor.is_symlink() or not is_trusted_directory(
            ancestor_metadata,
            trusted_owners=trusted_owners,
        ):
            raise ExecutableTrustError(
                f"{field} executable has an unsafe ancestor path."
            )
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent

    try:
        with executable.open("rb") as stream:
            first_line = stream.readline(4097)
    except OSError:
        raise ExecutableTrustError(
            f"{field} executable could not be inspected."
        ) from None
    if not first_line.startswith(b"#!"):
        return
    if len(first_line) > 4096 and not first_line.endswith(b"\n"):
        raise ExecutableTrustError(f"{field} executable has an invalid shebang.")
    try:
        shebang = first_line[2:].strip().decode("utf-8")
    except UnicodeDecodeError:
        raise ExecutableTrustError(
            f"{field} executable has an invalid shebang."
        ) from None
    tokens = shebang.split()
    if not tokens:
        raise ExecutableTrustError(f"{field} executable has an invalid shebang.")
    if len(tokens) != 1:
        raise ExecutableTrustError(
            f"{field} executable shebang must contain only one absolute interpreter "
            "path; interpreter arguments are not allowed."
        )
    interpreter = Path(tokens[0])
    if interpreter == Path("/usr/bin/env"):
        raise ExecutableTrustError(
            f"{field} must not use /usr/bin/env in its shebang for scheduled runs."
        )
    if not interpreter.is_absolute():
        raise ExecutableTrustError(
            f"{field} executable must use an absolute shebang interpreter."
        )
    interpreter = _resolve_trusted_interpreter(interpreter, field=field)
    _require_trusted_executable(
        interpreter,
        field=field,
        seen=seen | {executable},
    )


def _resolve_trusted_interpreter(interpreter: Path, *, field: str) -> Path:
    """Resolve only operator- or root-owned symlinks in an interpreter path."""

    trusted_owners = _trusted_owners()
    current = Path(interpreter.anchor)
    for part in interpreter.parts[1:]:
        current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError:
            raise ExecutableTrustError(
                f"{field} interpreter path is unavailable or unsafe."
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            if metadata.st_uid not in trusted_owners:
                raise ExecutableTrustError(
                    f"{field} interpreter path is unavailable or unsafe."
                )
            continue
        if current != interpreter and not is_trusted_directory(
            metadata,
            trusted_owners=trusted_owners,
        ):
            raise ExecutableTrustError(
                f"{field} interpreter path is unavailable or unsafe."
            )
    try:
        return interpreter.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ExecutableTrustError(
            f"{field} interpreter path is unavailable or unsafe."
        ) from None


__all__ = [
    "ExecutableTrustError",
    "is_indirect_command_launcher",
    "is_supported_direct_helper_command",
    "require_absolute_trusted_direct_executable",
    "require_absolute_trusted_executable",
    "require_absolute_trusted_wrapper",
]
