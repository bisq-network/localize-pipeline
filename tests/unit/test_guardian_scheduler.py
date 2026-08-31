from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from localize.guardian.scheduler import (
    LaunchdSchedule,
    SchedulerError,
    is_run_due,
    render_launchd_plist,
    render_launchd_runner,
)


def test_launchd_plist_catches_login_and_wake_without_embedding_secrets(tmp_path):
    schedule = LaunchdSchedule(
        label="org.example.localize-guardian",
        runner_path=tmp_path / "runner & guardian.sh",
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        interval_seconds=900,
    )

    plist = render_launchd_plist(schedule)

    assert "<key>RunAtLoad</key>\n    <true/>" in plist
    assert "<key>StartInterval</key>\n    <integer>900</integer>" in plist
    assert "runner &amp; guardian.sh" in plist
    assert "OPENAI_API_KEY" not in plist
    assert "GITHUB_TOKEN" not in plist
    assert "EnvironmentVariables" not in plist


def test_launchd_schedule_rejects_rapid_polling_and_relative_paths(tmp_path):
    with pytest.raises(SchedulerError, match="at least 300"):
        LaunchdSchedule(
            label="org.example.guardian",
            runner_path=tmp_path / "runner",
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            interval_seconds=60,
        )

    with pytest.raises(SchedulerError, match="absolute"):
        LaunchdSchedule(
            label="org.example.guardian",
            runner_path=Path("runner"),
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
        )


def test_runner_uses_argv_quoting_and_contains_no_secret_values(tmp_path):
    executable = tmp_path / "bin with spaces" / "localize"
    config = tmp_path / "guardian config.yaml"

    runner = render_launchd_runner(executable=executable, config_path=config)

    assert "exec '" in runner
    assert str(executable) in runner
    assert "guardian' 'run' '--scheduled' '--config'" in runner
    assert str(config) in runner
    assert "OPENAI_API_KEY=" not in runner
    assert "GITHUB_TOKEN=" not in runner


def test_runner_rejects_newline_in_paths(tmp_path):
    with pytest.raises(SchedulerError, match="newline"):
        render_launchd_runner(
            executable=Path("/opt/localize\nmalicious"),
            config_path=tmp_path / "config.yaml",
        )


def test_daily_due_catches_up_after_sleep_and_runs_only_once():
    now = datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)

    assert is_run_due(now=now, last_success=None, hour=8, minute=7)
    assert not is_run_due(
        now=now,
        last_success=datetime(2026, 8, 30, 8, 8, tzinfo=timezone.utc),
        hour=8,
        minute=7,
    )
    assert is_run_due(
        now=now,
        last_success=now - timedelta(days=1, minutes=1),
        hour=8,
        minute=7,
    )


def test_daily_due_before_schedule_waits_for_today():
    now = datetime(2026, 8, 30, 7, 30, tzinfo=timezone.utc)

    assert not is_run_due(now=now, last_success=None, hour=8, minute=7)


@pytest.mark.parametrize("hour,minute", [(-1, 0), (24, 0), (8, -1), (8, 60)])
def test_daily_due_rejects_invalid_clock(hour, minute):
    with pytest.raises(SchedulerError):
        is_run_due(
            now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
            last_success=None,
            hour=hour,
            minute=minute,
        )
