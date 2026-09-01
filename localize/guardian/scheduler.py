"""Portable scheduling helpers for the self-hosted guardian.

The scheduler contains no credentials or project policy.  It only invokes the
installed ``localize`` executable periodically; the guardian state decides
whether the configured daily run is due.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from xml.sax.saxutils import escape


class SchedulerError(ValueError):
    """Raised when scheduler input would create an unsafe installation."""


def _absolute_path(path: Path, *, field: str) -> Path:
    value = str(path)
    if "\n" in value or "\r" in value:
        raise SchedulerError(f"{field} must not contain a newline.")
    if not path.is_absolute():
        raise SchedulerError(f"{field} must be an absolute path.")
    return path


@dataclass(frozen=True)
class LaunchdSchedule:
    """Values needed to render one secret-free launchd property list."""

    label: str
    runner_path: Path
    stdout_path: Path
    stderr_path: Path
    interval_seconds: int = 900

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.label):
            raise SchedulerError("launchd label may contain only letters, digits, dots, and hyphens.")
        for field in ("runner_path", "stdout_path", "stderr_path"):
            _absolute_path(getattr(self, field), field=field)
        if self.interval_seconds < 300:
            raise SchedulerError("launchd interval must be at least 300 seconds.")


def render_launchd_plist(schedule: LaunchdSchedule) -> str:
    """Render a launchd plist without environment variables or credentials."""
    values = {
        "label": escape(schedule.label),
        "runner": escape(str(schedule.runner_path)),
        "stdout": escape(str(schedule.stdout_path)),
        "stderr": escape(str(schedule.stderr_path)),
    }
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{values['label']}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{values['runner']}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>{schedule.interval_seconds}</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>{values['stdout']}</string>
    <key>StandardErrorPath</key>
    <string>{values['stderr']}</string>
</dict>
</plist>
"""


def _shell_literal(value: str, *, field: str) -> str:
    if "\n" in value or "\r" in value:
        raise SchedulerError(f"{field} must not contain a newline.")
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_launchd_runner(*, executable: Path, config_path: Path) -> str:
    """Render the small argv-only wrapper staged outside TCC-protected paths."""
    executable = _absolute_path(executable, field="executable")
    config_path = _absolute_path(config_path, field="config_path")
    argv = (
        str(executable),
        "guardian",
        "run",
        "--scheduled",
        "--config",
        str(config_path),
    )
    command = " ".join(_shell_literal(value, field="argument") for value in argv)
    return f"#!/bin/sh\nset -eu\nexec {command}\n"


def is_run_due(
    *,
    now: datetime,
    last_success: datetime | None,
    hour: int,
    minute: int,
) -> bool:
    """Return whether today's scheduled run is due, including wake catch-up."""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise SchedulerError("scheduled hour/minute is outside the valid clock range.")
    if now.tzinfo is None or (last_success is not None and last_success.tzinfo is None):
        raise SchedulerError("scheduler timestamps must be timezone-aware.")

    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False
    if last_success is None:
        return True
    return last_success.astimezone(now.tzinfo) < scheduled
