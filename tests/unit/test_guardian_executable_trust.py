"""Executable and interpreter-chain trust checks for scheduled Guardian runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from localize.guardian.executable_trust import (
    ExecutableTrustError,
    is_supported_direct_helper_command,
    require_absolute_trusted_direct_executable,
    require_absolute_trusted_executable,
    require_absolute_trusted_wrapper,
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


@pytest.mark.parametrize(
    "name",
    ("python3.11", "node", "nice", "nohup"),
)
def test_direct_helper_rejects_interpreters_and_dispatchers(
    tmp_path: Path,
    name: str,
) -> None:
    launcher = tmp_path / name
    _write_executable(launcher, "native-test-binary")

    with pytest.raises(ExecutableTrustError, match="directly"):
        require_absolute_trusted_direct_executable(
            (str(launcher), "/unchecked/helper"),
            field="credential helper",
        )


def test_direct_helper_accepts_an_inspected_executable_script(tmp_path: Path) -> None:
    helper = tmp_path / "credential-helper"
    _write_executable(helper, "#!/bin/sh\nexit 0\n")

    require_absolute_trusted_direct_executable((str(helper),), field="helper")


@pytest.mark.parametrize(
    "command",
    (
        ("/usr/bin/awk", "-f", "/unchecked/helper.awk"),
        ("/usr/bin/tclsh", "/unchecked/helper.tcl"),
        ("/opt/homebrew/bin/uv", "run", "/unchecked/helper.py"),
        ("/opt/helpers/credential", "read"),
    ),
)
def test_direct_helper_contract_rejects_every_custom_argument(
    command: tuple[str, ...],
) -> None:
    assert not is_supported_direct_helper_command(command)


def test_direct_helper_contract_allows_only_exact_github_cli_exception() -> None:
    assert is_supported_direct_helper_command(
        ("/usr/local/bin/gh", "auth", "token"),
        allow_github_cli=True,
    )
    assert not is_supported_direct_helper_command(
        ("/usr/local/bin/gh", "auth", "status"),
        allow_github_cli=True,
    )


@pytest.mark.parametrize(
    "validator",
    (
        require_absolute_trusted_direct_executable,
        require_absolute_trusted_wrapper,
    ),
)
def test_direct_helpers_reject_shebang_interpreter_arguments(
    tmp_path: Path,
    validator,
) -> None:
    helper = tmp_path / "helper"
    _write_executable(helper, "#!/bin/sh -e\nexit 0\n")

    with pytest.raises(ExecutableTrustError, match="interpreter arguments"):
        validator((str(helper),), field="helper")


def test_sandbox_wrapper_rejects_unchecked_policy_or_script_arguments(
    tmp_path: Path,
) -> None:
    wrapper = tmp_path / "sandbox-wrapper"
    _write_executable(wrapper, "#!/bin/sh\nexit 0\n")

    with pytest.raises(ExecutableTrustError, match="exactly one direct wrapper"):
        require_absolute_trusted_wrapper(
            (str(wrapper), "--profile", "/unchecked/policy"),
            field="sandbox",
        )
