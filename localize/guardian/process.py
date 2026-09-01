"""Bounded subprocess execution for model and untrusted-code boundaries."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path, PurePosixPath
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
    """POSIX limits and optional Linux process-tree containment."""

    cpu_seconds: int
    max_file_size_bytes: int
    max_open_files: int = 256
    max_processes: int | None = None
    require_linux_cgroup: bool = False

    @classmethod
    def for_timeout(
        cls,
        timeout_seconds: float,
        *,
        max_file_size_bytes: int,
        require_linux_cgroup: bool = False,
    ) -> "ProcessLimits":
        if timeout_seconds <= 0 or max_file_size_bytes <= 0:
            raise ValueError("Process resource limits must be positive.")
        process_limit = 64 if platform.system() == "Linux" else None
        return cls(
            cpu_seconds=max(1, math.ceil(timeout_seconds) + 5),
            max_file_size_bytes=max_file_size_bytes,
            max_processes=process_limit,
            require_linux_cgroup=require_linux_cgroup,
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


_CGROUP_V2_ROOT = Path("/sys/fs/cgroup")
_CGROUP_DRAIN_TIMEOUT_SECONDS = 2.0
_CGROUP_LEAF_CONTROLS = (
    "cgroup.events",
    "cgroup.kill",
    "cgroup.max.depth",
    "cgroup.max.descendants",
    "cgroup.procs",
)


def _current_linux_cgroup_parent() -> Path:
    """Return this process's cgroup-v2 directory without trusting the environment."""

    try:
        raw_membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProcessResourceError(
            "Linux cgroup v2 membership could not be inspected."
        ) from exc
    if len(raw_membership) > 64 * 1024:
        raise ProcessResourceError("Linux cgroup v2 membership is malformed.")

    relative: PurePosixPath | None = None
    for line in raw_membership.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            raw_path = fields[2]
            components = [] if raw_path == "/" else raw_path.split("/")[1:]
            if (
                not raw_path.startswith("/")
                or "\x00" in raw_path
                or any(part in {"", ".", ".."} for part in components)
            ):
                raise ProcessResourceError(
                    "Linux cgroup v2 membership is malformed."
                )
            relative = PurePosixPath(*components)
            break
    if relative is None:
        raise ProcessResourceError("Linux cgroup v2 is unavailable.")

    try:
        root = _CGROUP_V2_ROOT.resolve(strict=True)
        if _CGROUP_V2_ROOT.is_symlink() or not (root / "cgroup.controllers").is_file():
            raise ProcessResourceError("Linux cgroup v2 is unavailable.")
        parent = (root / Path(*relative.parts)).resolve(strict=True)
        parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ProcessResourceError(
            "Linux cgroup v2 membership could not be resolved safely."
        ) from exc
    if not parent.is_dir() or parent.is_symlink():
        raise ProcessResourceError(
            "Linux cgroup v2 membership could not be resolved safely."
        )
    return parent


def linux_cgroup_parent_procs() -> Path | None:
    """Return the Linux migration escape target that a sandbox must deny."""

    if platform.system() != "Linux":
        return None
    parent = _current_linux_cgroup_parent()
    control = parent / "cgroup.procs"
    if not control.is_file() or control.is_symlink():
        raise ProcessResourceError(
            "Linux cgroup v2 migration controls could not be inspected safely."
        )
    return control


def _open_cgroup_control(path: Path, flags: int) -> int:
    safe_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, safe_flags)


def _write_cgroup_control(path: Path, payload: bytes, *, failure: str) -> None:
    try:
        descriptor = _open_cgroup_control(path, os.O_WRONLY)
        try:
            written = os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ProcessResourceError(failure) from exc
    if written != len(payload):
        raise ProcessResourceError(failure)


class _LinuxCgroupV2Scope:
    """One cgroup-v2 leaf whose kernel kill switch includes escaped sessions."""

    def __init__(self, path: Path, procs_fd: int) -> None:
        self.path = path
        self._procs_fd: int | None = procs_fd
        self._closed = False
        self._kill_requested = False

    @classmethod
    def create(cls) -> "_LinuxCgroupV2Scope":
        parent = _current_linux_cgroup_parent()
        try:
            raw_path = tempfile.mkdtemp(prefix="localize-guardian-", dir=parent)
            path = Path(raw_path)
        except OSError as exc:
            raise ProcessResourceError(
                "Linux cgroup v2 is not delegated to the Guardian operator."
            ) from exc
        try:
            path.resolve(strict=True).relative_to(parent)
            for control_name in _CGROUP_LEAF_CONTROLS:
                if not (path / control_name).is_file():
                    raise ProcessResourceError(
                        "Linux cgroup v2 lacks the required process-tree controls."
                    )
            for control_name in ("cgroup.max.depth", "cgroup.max.descendants"):
                _write_cgroup_control(
                    path / control_name,
                    b"0\n",
                    failure="Linux cgroup v2 could not seal its containment leaf.",
                )
            procs_fd = _open_cgroup_control(path / "cgroup.procs", os.O_WRONLY)
        except Exception:
            try:
                os.rmdir(path)
            except OSError:
                pass
            raise
        return cls(path, procs_fd)

    @property
    def procs_fd(self) -> int:
        if self._procs_fd is None:
            raise ProcessResourceError("Linux cgroup join handle is closed.")
        return self._procs_fd

    def close_join_handle(self) -> None:
        if self._procs_fd is not None:
            os.close(self._procs_fd)
            self._procs_fd = None

    def kill_all(self) -> None:
        if self._kill_requested or self._closed:
            return
        _write_cgroup_control(
            self.path / "cgroup.kill",
            b"1\n",
            failure="Linux cgroup v2 could not terminate the bounded process tree.",
        )
        self._kill_requested = True

    def _is_empty(self) -> bool:
        try:
            events = (self.path / "cgroup.events").read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProcessResourceError(
                "Linux cgroup v2 completion could not be inspected."
            ) from exc
        values = {
            key: value
            for line in events.splitlines()
            if len(fields := line.split()) == 2
            for key, value in (fields,)
        }
        populated = values.get("populated")
        if populated not in {"0", "1"}:
            raise ProcessResourceError(
                "Linux cgroup v2 completion state is malformed."
            )
        return populated == "0"

    def close(self) -> None:
        if self._closed:
            return
        self.close_join_handle()
        self.kill_all()
        deadline = time.monotonic() + _CGROUP_DRAIN_TIMEOUT_SECONDS
        while not self._is_empty():
            if time.monotonic() >= deadline:
                raise ProcessResourceError(
                    "Linux cgroup v2 process tree did not terminate."
                )
            time.sleep(0.01)
        try:
            os.rmdir(self.path)
        except OSError as exc:
            raise ProcessResourceError(
                "Linux cgroup v2 scope could not be removed."
            ) from exc
        self._closed = True


def _prepare_posix_child(
    limits: ProcessLimits,
    cgroup_procs_fd: int | None,
) -> None:
    if cgroup_procs_fd is not None:
        try:
            if os.write(cgroup_procs_fd, b"0\n") != 2:
                raise OSError(errno.EIO, "short cgroup.procs write")
        finally:
            os.close(cgroup_procs_fd)
    _limit_child(limits)


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
    """Run an argv with bounded resources and Linux cgroup tree cleanup."""

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

        cgroup_scope: _LinuxCgroupV2Scope | None = None
        if limits.require_linux_cgroup and platform.system() == "Linux":
            cgroup_scope = _LinuxCgroupV2Scope.create()
            resources.callback(cgroup_scope.close)
        cgroup_procs_fd = (
            cgroup_scope.procs_fd if cgroup_scope is not None else None
        )
        try:
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
                pass_fds=(
                    (cgroup_procs_fd,)
                    if cgroup_procs_fd is not None and os.name == "posix"
                    else ()
                ),
                preexec_fn=(
                    lambda: _prepare_posix_child(limits, cgroup_procs_fd)
                )
                if os.name == "posix"
                else None,
            )
        finally:
            if cgroup_scope is not None:
                cgroup_scope.close_join_handle()
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
                    if cgroup_scope is not None:
                        cgroup_scope.kill_all()
                    _kill_process_group(process)
                    process.communicate()
                    raise subprocess.TimeoutExpired(argv, timeout)
                if exit_watcher is not None:
                    if exit_watcher.wait(min(0.05, remaining)):
                        if cgroup_scope is not None:
                            cgroup_scope.kill_all()
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
                        if cgroup_scope is not None:
                            cgroup_scope.kill_all()
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
                if cgroup_scope is not None:
                    cgroup_scope.kill_all()
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
