"""Bounded subprocess execution for model and untrusted-code boundaries."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import math
import os
from pathlib import Path
import platform
import resource
import select
import signal
import stat
import subprocess
import tempfile
import time
from typing import IO, Mapping, Sequence


class ProcessResourceError(RuntimeError):
    """A child exceeded a Guardian-owned resource boundary."""


_WORKSPACE_QUOTA_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    """POSIX limits inherited by every descendant in the process group."""

    cpu_seconds: int
    max_file_size_bytes: int
    max_open_files: int = 256
    max_processes: int | None = None

    @classmethod
    def for_timeout(
        cls,
        timeout_seconds: float,
        *,
        max_file_size_bytes: int,
    ) -> "ProcessLimits":
        if timeout_seconds <= 0 or max_file_size_bytes <= 0:
            raise ValueError("Process resource limits must be positive.")
        process_limit = 64 if platform.system() == "Linux" else None
        return cls(
            cpu_seconds=max(1, math.ceil(timeout_seconds) + 5),
            max_file_size_bytes=max_file_size_bytes,
            max_processes=process_limit,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceQuota:
    """Bound aggregate directory growth while untrusted code is executing."""

    root: Path
    baseline_bytes: int
    baseline_entries: int
    max_growth_bytes: int
    max_added_entries: int

    @classmethod
    def capture(
        cls,
        root: Path,
        *,
        max_growth_bytes: int,
        max_added_entries: int,
    ) -> "WorkspaceQuota":
        resolved = root.resolve(strict=True)
        if not resolved.is_dir() or max_growth_bytes <= 0 or max_added_entries <= 0:
            raise ValueError("Workspace quota configuration is invalid.")
        total, entries = _directory_usage(resolved)
        return cls(
            root=resolved,
            baseline_bytes=total,
            baseline_entries=entries,
            max_growth_bytes=max_growth_bytes,
            max_added_entries=max_added_entries,
        )

    def exceeded(self) -> bool:
        total, entries = _directory_usage(
            self.root,
            stop_after_bytes=self.baseline_bytes + self.max_growth_bytes,
            stop_after_entries=self.baseline_entries + self.max_added_entries,
        )
        return (
            total > self.baseline_bytes + self.max_growth_bytes
            or entries > self.baseline_entries + self.max_added_entries
        )


def _directory_usage(
    root: Path,
    *,
    stop_after_bytes: int | None = None,
    stop_after_entries: int | None = None,
) -> tuple[int, int]:
    total = 0
    entries = 0
    try:
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if not (Path(current) / name).is_symlink()
            ]
            for name in (*directories, *filenames):
                try:
                    metadata = (Path(current) / name).lstat()
                except FileNotFoundError:
                    continue
                entries += 1
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
                if (
                    stop_after_bytes is not None
                    and total > stop_after_bytes
                    or stop_after_entries is not None
                    and entries > stop_after_entries
                ):
                    return total, entries
    except OSError as exc:
        raise ProcessResourceError("Could not inspect the bounded workspace quota.") from exc
    return total, entries


def _bounded_limit(resource_name: int, requested: int) -> tuple[int, int]:
    _soft, hard = resource.getrlimit(resource_name)
    value = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
    return value, value


def _limit_child(limits: ProcessLimits) -> None:
    resource.setrlimit(
        resource.RLIMIT_CPU,
        _bounded_limit(resource.RLIMIT_CPU, limits.cpu_seconds),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        _bounded_limit(resource.RLIMIT_FSIZE, limits.max_file_size_bytes),
    )
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        _bounded_limit(resource.RLIMIT_NOFILE, limits.max_open_files),
    )
    if limits.max_processes is not None and hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(
            resource.RLIMIT_NPROC,
            _bounded_limit(resource.RLIMIT_NPROC, limits.max_processes),
        )


def _kill_process_group(
    process: subprocess.Popen[object],
    *,
    tolerate_exited_leader: bool = False,
) -> None:
    if os.name == "posix":
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if not tolerate_exited_leader:
                raise
    elif process.poll() is None:  # pragma: no cover - Windows is not a deploy target.
        process.kill()


class _UnreapedExitWatcher:
    """Observe POSIX child exit while retaining ownership of its process id."""

    def __init__(self, pid: int) -> None:
        self._pidfd: int | None = None
        self._poller = None
        self._kqueue = None
        self._ready = False
        try:
            if hasattr(os, "pidfd_open"):
                self._pidfd = os.pidfd_open(pid)  # type: ignore[attr-defined]
                self._poller = select.poll()
                self._poller.register(self._pidfd, select.POLLIN)
            elif hasattr(select, "kqueue"):
                self._kqueue = select.kqueue()
                event = select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=(
                        select.KQ_EV_ADD
                        | select.KQ_EV_ENABLE
                        | select.KQ_EV_ONESHOT
                    ),
                    fflags=select.KQ_NOTE_EXIT,
                )
                self._kqueue.control([event], 0, 0)
            else:  # pragma: no cover - supported production targets have one.
                raise ProcessResourceError(
                    "Safe unreaped child monitoring is unavailable."
                )
        except ProcessLookupError:
            self._ready = True
        except OSError as exc:
            self.close()
            raise ProcessResourceError(
                "Could not establish safe child-process monitoring."
            ) from exc

    def wait(self, timeout: float) -> bool:
        """Return whether the child exited, without reaping its process id."""

        if self._ready:
            return True
        if self._poller is not None:
            events = self._poller.poll(max(1, math.ceil(timeout * 1000)))
        else:
            assert self._kqueue is not None
            events = self._kqueue.control(None, 1, timeout)
        self._ready = bool(events)
        return self._ready

    def close(self) -> None:
        """Release platform watcher resources without touching the child."""

        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None
        if self._pidfd is not None:
            os.close(self._pidfd)
            self._pidfd = None


def run_bounded_process(
    argv: Sequence[str],
    *,
    input: str | bytes | None = None,
    text: bool = False,
    capture_output: bool = False,
    check: bool = False,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    shell: bool = False,
    stdin: int | IO[object] | None = None,
    stdout: int | IO[object] | None = None,
    stderr: int | IO[object] | None = None,
    encoding: str | None = None,
    errors: str | None = None,
    start_new_session: bool = True,
    limits: ProcessLimits,
    workspace_quota: WorkspaceQuota | None = None,
) -> subprocess.CompletedProcess:
    """Run an argv with hard descendant cleanup and bounded filesystem growth."""

    if not argv or timeout <= 0 or shell or not start_new_session:
        raise ValueError("Bounded processes require argv, a timeout, and a new session.")
    if capture_output and (stdout is not None or stderr is not None):
        raise ValueError("capture_output cannot be combined with stdout or stderr.")
    if input is not None and stdin is not None:
        raise ValueError("stdin and input arguments may not both be used.")
    if text and encoding is None:
        encoding = "utf-8"

    with ExitStack() as resources:
        stdout_file: IO[bytes] | None = None
        stderr_file: IO[bytes] | None = None
        if capture_output:
            stdout_file = resources.enter_context(tempfile.TemporaryFile())
            stderr_file = resources.enter_context(tempfile.TemporaryFile())
            stdout = stdout_file
            stderr = stderr_file
        if input is not None:
            if text:
                input_file = resources.enter_context(
                    tempfile.TemporaryFile(
                        mode="w+t",
                        encoding=encoding or "utf-8",
                        errors=errors,
                    )
                )
            else:
                input_file = resources.enter_context(tempfile.TemporaryFile())
            input_file.write(input)
            input_file.flush()
            input_file.seek(0)
            stdin = input_file

        process = subprocess.Popen(
            list(argv),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=None if env is None else dict(env),
            shell=False,
            start_new_session=True,
            text=text,
            encoding=encoding,
            errors=errors,
            preexec_fn=(lambda: _limit_child(limits)) if os.name == "posix" else None,
        )
        exit_watcher: _UnreapedExitWatcher | None = None
        try:
            started_at = time.monotonic()
            deadline = started_at + timeout
            next_quota_check = started_at
            captured_stdout = None
            captured_stderr = None
            if os.name == "posix":
                exit_watcher = _UnreapedExitWatcher(process.pid)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_process_group(process)
                    process.communicate()
                    raise subprocess.TimeoutExpired(argv, timeout)
                if exit_watcher is not None:
                    if exit_watcher.wait(min(0.05, remaining)):
                        _kill_process_group(process, tolerate_exited_leader=True)
                        captured_stdout, captured_stderr = process.communicate()
                        break
                else:  # pragma: no cover - Windows is not a deploy target.
                    try:
                        captured_stdout, captured_stderr = process.communicate(
                            timeout=min(0.05, remaining),
                        )
                        break
                    except subprocess.TimeoutExpired:
                        pass
                quota_check_at = time.monotonic()
                if (
                    workspace_quota is not None
                    and quota_check_at >= next_quota_check
                ):
                    next_quota_check = (
                        quota_check_at + _WORKSPACE_QUOTA_INTERVAL_SECONDS
                    )
                    if workspace_quota.exceeded():
                        _kill_process_group(process)
                        process.communicate()
                        raise ProcessResourceError(
                            "Child process exceeded its workspace quota."
                        )
            if workspace_quota is not None and workspace_quota.exceeded():
                raise ProcessResourceError(
                    "Child process exceeded its workspace quota."
                )
        finally:
            if exit_watcher is not None:
                exit_watcher.close()
            if process.returncode is None:
                _kill_process_group(process)
                process.communicate()

        if capture_output:
            assert stdout_file is not None and stderr_file is not None
            stdout_file.seek(0)
            stderr_file.seek(0)
            raw_stdout = stdout_file.read(limits.max_file_size_bytes + 1)
            raw_stderr = stderr_file.read(limits.max_file_size_bytes + 1)
            if (
                len(raw_stdout) > limits.max_file_size_bytes
                or len(raw_stderr) > limits.max_file_size_bytes
            ):
                raise ProcessResourceError("Child process exceeded its output quota.")
            if text:
                codec = encoding or "utf-8"
                captured_stdout = raw_stdout.decode(codec, errors or "strict")
                captured_stderr = raw_stderr.decode(codec, errors or "strict")
            else:
                captured_stdout = raw_stdout
                captured_stderr = raw_stderr

        completed = subprocess.CompletedProcess(
            list(argv),
            process.returncode,
            captured_stdout,
            captured_stderr,
        )
        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode,
                completed.args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed
