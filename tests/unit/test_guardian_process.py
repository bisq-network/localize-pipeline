from __future__ import annotations

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
