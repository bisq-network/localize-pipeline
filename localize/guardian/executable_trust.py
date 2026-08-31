"""Shared executable-integrity checks for unattended Guardian processes."""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import stat


class ExecutableTrustError(ValueError):
    """A configured executable or interpreter chain is mutable or ambiguous."""


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
    trusted_owners = {0}
    if hasattr(os, "getuid"):
        trusted_owners.add(os.getuid())
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
        if (
            ancestor.is_symlink()
            or not stat.S_ISDIR(ancestor_metadata.st_mode)
            or ancestor_metadata.st_uid not in trusted_owners
            or stat.S_IMODE(ancestor_metadata.st_mode) & 0o022
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
    interpreter = Path(tokens[0])
    if interpreter == Path("/usr/bin/env"):
        raise ExecutableTrustError(
            f"{field} must not use /usr/bin/env in its shebang for scheduled runs."
        )
    if not interpreter.is_absolute():
        raise ExecutableTrustError(
            f"{field} executable must use an absolute shebang interpreter."
        )
    _require_trusted_executable(
        interpreter,
        field=field,
        seen=seen | {executable},
    )


__all__ = ["ExecutableTrustError", "require_absolute_trusted_executable"]
