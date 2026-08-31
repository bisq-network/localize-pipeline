"""Executable and interpreter-chain trust checks for scheduled Guardian runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from localize.guardian.executable_trust import (
    ExecutableTrustError,
    require_absolute_trusted_executable,
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def test_trusted_absolute_shebang_interpreter_may_be_a_symlink(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "interpreter-real"
    _write_executable(interpreter, "native-test-binary")
    interpreter_link = tmp_path / "interpreter-link"
    interpreter_link.symlink_to(interpreter)
    executable = tmp_path / "helper"
    _write_executable(executable, f"#!{interpreter_link}\n")

    require_absolute_trusted_executable((str(executable),), field="helper")


def test_shebang_interpreter_symlink_under_writable_parent_is_rejected(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    interpreter = unsafe / "interpreter-real"
    _write_executable(interpreter, "native-test-binary")
    interpreter_link = unsafe / "interpreter-link"
    interpreter_link.symlink_to(interpreter)
    unsafe.chmod(0o720)
    executable = tmp_path / "helper"
    _write_executable(executable, f"#!{interpreter_link}\n")

    with pytest.raises(ExecutableTrustError, match="interpreter"):
        require_absolute_trusted_executable((str(executable),), field="helper")
