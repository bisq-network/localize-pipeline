from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from localize.guardian import process as guardian_process
from localize.guardian.process import (
    ProcessLimits,
    ProcessResourceError,
    WorkspaceQuota,
    run_bounded_process,
)


def _assert_file_stops_changing(path: Path) -> None:
    size = path.stat().st_size
    time.sleep(0.15)
    assert path.stat().st_size == size


def test_kills_descendants_when_the_direct_child_exits(tmp_path: Path) -> None:
    marker = tmp_path / "heartbeat"
    child = (
        "import pathlib,sys,time; "
        "p=pathlib.Path(sys.argv[1]); p.write_text('x'); "
        "[(p.open('a').write('x'), time.sleep(.02)) for _ in iter(int, 1)]"
    )
    parent = """
import pathlib
import subprocess
import sys
import time

p = subprocess.Popen(
    [sys.executable, "-c", sys.argv[2], sys.argv[1]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 2
while not pathlib.Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print(p.pid, flush=True)
"""

    completed = run_bounded_process(
        (sys.executable, "-c", parent, str(marker), child),
        capture_output=True,
        text=True,
        timeout=5,
        limits=ProcessLimits.for_timeout(5, max_file_size_bytes=1024 * 1024),
    )

    assert completed.returncode == 0
    assert completed.stdout.strip().isdigit()
    _assert_file_stops_changing(marker)


def test_timeout_kills_the_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "heartbeat"
    child = (
        "import pathlib,sys,time; "
        "p=pathlib.Path(sys.argv[1]); "
        "[(p.open('a').write('x'), time.sleep(.02)) for _ in iter(int, 1)]"
    )
    parent = """
import pathlib
import subprocess
import sys
import time

subprocess.Popen(
    [sys.executable, "-c", sys.argv[2], sys.argv[1]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not pathlib.Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(30)
"""

    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process(
            (sys.executable, "-c", parent, str(marker), child),
            timeout=2,
            limits=ProcessLimits.for_timeout(
                1,
                max_file_size_bytes=1024 * 1024,
            ),
        )

    assert marker.exists()
    _assert_file_stops_changing(marker)


def test_process_limits_can_require_linux_cgroup_containment() -> None:
    limits = ProcessLimits.for_timeout(
        5,
        max_file_size_bytes=1024 * 1024,
        require_linux_cgroup=True,
    )

    assert limits.require_linux_cgroup is True


def test_cgroup_escape_target_is_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guardian_process.platform, "system", lambda: "Darwin")

    assert guardian_process.linux_cgroup_parent_procs() is None


def test_cgroup_escape_target_is_the_current_parent_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "guardian"
    parent.mkdir()
    control = parent / "cgroup.procs"
    control.touch()
    monkeypatch.setattr(guardian_process.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        guardian_process,
        "_current_linux_cgroup_parent",
        lambda: parent,
    )

    assert guardian_process.linux_cgroup_parent_procs() == control


def test_cgroup_completion_rejects_a_missing_population_state(tmp_path: Path) -> None:
    (tmp_path / "cgroup.events").write_text("frozen 0\n", encoding="ascii")
    procs_fd = os.open(os.devnull, os.O_WRONLY)
    scope = guardian_process._LinuxCgroupV2Scope(tmp_path, procs_fd)

    try:
        with pytest.raises(ProcessResourceError, match="state is malformed"):
            scope._is_empty()
    finally:
        scope.close_join_handle()


def test_required_linux_cgroup_failure_prevents_target_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "must-not-exist"

    def fail_create(_cls: type[object]) -> None:
        raise ProcessResourceError("cgroup unavailable")

    monkeypatch.setattr(guardian_process.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        guardian_process._LinuxCgroupV2Scope,
        "create",
        classmethod(fail_create),
    )

    with pytest.raises(ProcessResourceError, match="cgroup unavailable"):
        run_bounded_process(
            (
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ),
            timeout=5,
            limits=ProcessLimits.for_timeout(
                5,
                max_file_size_bytes=1024 * 1024,
                require_linux_cgroup=True,
            ),
        )

    assert not marker.exists()


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux cgroup v2")
def test_linux_cgroup_kills_a_setsid_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_delegated_parent = os.environ.get("LOCALIZE_GUARDIAN_TEST_CGROUP")
    if raw_delegated_parent is None:
        pytest.skip("no delegated cgroup v2 parent available")
    delegated_parent = Path(raw_delegated_parent).resolve(strict=True)
    marker = tmp_path / "escaped-heartbeat"
    child = (
        "import os,pathlib,sys,time; os.setsid(); "
        "p=pathlib.Path(sys.argv[1]); "
        "[(p.open('a').write('x'), time.sleep(.02)) for _ in iter(int, 1)]"
    )
    parent = """
import pathlib
import subprocess
import sys
import time

p = subprocess.Popen(
    [sys.executable, "-c", sys.argv[2], sys.argv[1]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 2
while not pathlib.Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
membership = next(
    line.split(":", 2)[2]
    for line in pathlib.Path("/proc/self/cgroup").read_text().splitlines()
    if line.startswith("0::")
)
scope = pathlib.Path("/sys/fs/cgroup" + membership)
print(
    p.pid,
    (scope / "cgroup.max.depth").read_text().strip(),
    (scope / "cgroup.max.descendants").read_text().strip(),
    flush=True,
)
"""
    monkeypatch.setattr(
        guardian_process,
        "_current_linux_cgroup_parent",
        lambda: delegated_parent,
    )

    completed = run_bounded_process(
        (sys.executable, "-c", parent, str(marker), child),
        capture_output=True,
        text=True,
        timeout=5,
        limits=ProcessLimits.for_timeout(
            5,
            max_file_size_bytes=1024 * 1024,
            require_linux_cgroup=True,
        ),
    )

    pid, max_depth, max_descendants = completed.stdout.split()
    assert pid.isdigit()
    assert (max_depth, max_descendants) == ("0", "0")
    assert marker.exists()
    _assert_file_stops_changing(marker)


def test_workspace_usage_ignores_entries_removed_during_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transient = tmp_path / "transient"
    transient.write_text("short-lived", encoding="utf-8")
    original_lstat = Path.lstat

    def racing_lstat(path: Path):
        if path == transient:
            raise FileNotFoundError(path)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", racing_lstat)

    assert guardian_process._directory_usage(tmp_path) == (0, 0)


@pytest.mark.skipif(guardian_process.os.name != "posix", reason="POSIX process groups")
def test_reaped_child_process_group_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        guardian_process.os,
        "killpg",
        lambda process_group, signal_number: calls.append(
            (process_group, signal_number)
        ),
    )
    reaped = SimpleNamespace(pid=12345, returncode=0)

    guardian_process._kill_process_group(reaped)  # type: ignore[arg-type]

    assert calls == []


def test_bounded_process_round_trips_text_stdin() -> None:
    completed = run_bounded_process(
        (sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"),
        input="guardian input",
        text=True,
        capture_output=True,
        timeout=5,
        limits=ProcessLimits.for_timeout(5, max_file_size_bytes=1024 * 1024),
    )

    assert completed.returncode == 0
    assert completed.stdout == "GUARDIAN INPUT\n"


def test_workspace_quota_walk_is_decoupled_from_short_exit_poll() -> None:
    class CountingQuota:
        def __init__(self) -> None:
            self.calls = 0

        def exceeded(self) -> bool:
            self.calls += 1
            return False

    quota = CountingQuota()

    completed = run_bounded_process(
        (sys.executable, "-c", "import time; time.sleep(0.3)"),
        timeout=2,
        limits=ProcessLimits.for_timeout(2, max_file_size_bytes=1024 * 1024),
        workspace_quota=quota,  # type: ignore[arg-type]
    )

    assert completed.returncode == 0
    assert quota.calls <= 2


def test_file_size_limit_bounds_one_child_output_file(tmp_path: Path) -> None:
    output = tmp_path / "large"
    completed = run_bounded_process(
        (
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'x'*(2**20))",
            str(output),
        ),
        timeout=5,
        limits=ProcessLimits.for_timeout(5, max_file_size_bytes=64 * 1024),
    )

    assert completed.returncode != 0
    assert output.stat().st_size <= 64 * 1024


def test_workspace_growth_quota_terminates_a_writer(tmp_path: Path) -> None:
    script = (
        "import pathlib,sys,time; root=pathlib.Path(sys.argv[1]); "
        "[(root / str(i)).write_bytes(b'x'*4096) or time.sleep(.01) "
        "for i in range(10000)]"
    )

    with pytest.raises(ProcessResourceError, match="workspace quota"):
        run_bounded_process(
            (sys.executable, "-c", script, str(tmp_path)),
            cwd=tmp_path,
            timeout=5,
            limits=ProcessLimits.for_timeout(5, max_file_size_bytes=64 * 1024),
            workspace_quota=WorkspaceQuota.capture(
                tmp_path,
                max_growth_bytes=32 * 1024,
                max_added_entries=32,
            ),
        )
