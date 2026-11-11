"""Exercise the deployed shell acquisition boundary without network or model calls."""
import os
from pathlib import Path
import shlex
import shutil
import time
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_source(tmp_path, outcomes, budget=3600, duration=2, source="transifex"):
    script = (ROOT / "update-translations.sh").read_text()
    # Include retry helpers when present, so this also reproduces the original bug.
    start = script.find("transifex_failure_is_retryable() {")
    if start < 0:
        start = script.index("prepare_translation_source() {")
    functions = script[start:script.index("\nTARGET_PROJECT_ROOT=", start)]
    for index, (code, message) in enumerate(outcomes, 1):
        (tmp_path / f"result-{index}").write_text(f"{code}\n{message}\n")
    harness = r'''
set -euo pipefail
log() { printf '%s\n' "$1"; }
log_cmd() { :; }
record_pipeline_event() { printf 'EVENT %s\n' "$*"; }
send_heartbeat_if_configured() { echo HEARTBEAT; }
command_exists() { return 0; }
translation_file_change_regex() { echo '\.properties$'; }
git() { :; }
which() { echo /stub/tx; }
# Each timeout invocation is an entire tx attempt; advance a virtual clock.
timeout() {
    local count=0
    if [[ -f "$LOG_DIR/count" ]]; then read -r count < "$LOG_DIR/count"; fi
    count=$((count+1))
    echo "$count" > "$LOG_DIR/count"
    printf '%s\n' "$*" >> "$LOG_DIR/timeouts"
    local code
    read -r code < "$LOG_DIR/result-$count"
    tail -n +2 "$LOG_DIR/result-$count"
    return "$code"
}
# SECONDS is advanced in the parent via the remaining-budget helper below.
sleep() {
    if (( BASH_SUBSHELL > 0 )); then exit 0; fi
    SECONDS=$((SECONDS + $1))
}
'''
    # timeout is piped, so account for attempt duration when logging its end.
    harness += f'''
LOG_DIR={shlex.quote(str(tmp_path))}
TARGET_PROJECT_ROOT="$LOG_DIR"
ABSOLUTE_INPUT_FOLDER="$LOG_DIR"
TX_PULL_TIMEOUT={budget}
SECONDS=0
log() {{
    printf '%s\\n' "$1"
    if [[ "$1" == Transifex\\ pull\\ attempt*finished* ]]; then
        SECONDS=$((SECONDS + {duration}))
    fi
}}
{functions}
prepare_translation_source {shlex.quote(source)} false false
echo DOWNSTREAM
'''
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={**os.environ, "OPENAI_API_KEY": "test"}, timeout=10)
    attempts = int((tmp_path / "count").read_text()) if (tmp_path / "count").exists() else 0
    return result, attempts


def test_retries_original_502_then_completes(tmp_path):
    result, attempts = run_source(tmp_path, [(1, "bisq-mobile.settings_properties - Response error code 502, retry after 10\nAborted"), (0, "Done")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts == 2
    assert "HEARTBEAT" in result.stdout
    assert len(list(tmp_path.glob("transifex-pull.*"))) == 2


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_exhaustion_is_bounded_and_blocks_downstream(tmp_path, code):
    result, attempts = run_source(tmp_path, [(1, f"Response error code {code}, retry after 10\nAborted")] * 4)
    assert result.returncode == 1
    assert attempts == 3
    assert "HEARTBEAT" not in result.stdout
    assert "DOWNSTREAM" not in result.stdout


@pytest.mark.parametrize("message", [
    "Response error code 401", "Response error code 403", "Response error code 404",
    "Response error code 501", "invalid configuration", "Aborted",
    "Response error code 502\nResponse error code 401", "Response error code 502\nInvalid token",
])
def test_permanent_or_unknown_error_is_not_retried(tmp_path, message):
    result, attempts = run_source(tmp_path, [(1, message)])
    assert result.returncode == 1
    assert attempts == 1
    assert "HEARTBEAT" not in result.stdout
    assert "DOWNSTREAM" not in result.stdout


@pytest.mark.parametrize("message", ["dial tcp: i/o timeout", "connection reset by peer", "TLS handshake timeout"])
def test_transient_transport_failure_recovers(tmp_path, message):
    result, attempts = run_source(tmp_path, [(1, message), (0, "Done")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts == 2


def test_timeout_does_not_start_another_attempt(tmp_path):
    result, attempts = run_source(tmp_path, [(124, "Response error code 502")])
    assert result.returncode == 124
    assert attempts == 1
    assert "HEARTBEAT" not in result.stdout


def test_backoff_cannot_exceed_shared_deadline(tmp_path):
    result, attempts = run_source(tmp_path, [(1, "Response error code 502")] * 4, budget=15)
    assert result.returncode == 124
    assert attempts == 1
    assert "HEARTBEAT" not in result.stdout


def test_each_attempt_receives_only_remaining_time(tmp_path):
    result, attempts = run_source(tmp_path, [(1, "Response error code 502"), (0, "Done")], budget=100)
    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts == 2
    commands = (tmp_path / "timeouts").read_text().splitlines()
    first = int(commands[0].split()[1])
    second = int(commands[1].split()[1])
    assert first == 100
    assert 58 <= second <= 68


def test_git_source_never_pulls(tmp_path):
    result, attempts = run_source(tmp_path, [], source="git")
    assert result.returncode == 0
    assert attempts == 0
    assert "DOWNSTREAM" in result.stdout


@pytest.mark.parametrize("status", [130, 137, 143])
def test_interruption_never_retries(tmp_path, status):
    result, attempts = run_source(tmp_path, [(status, "Response error code 502")])
    assert result.returncode == status
    assert attempts == 1
    assert "HEARTBEAT" not in result.stdout


def test_success_with_only_filtered_output_is_success(tmp_path):
    result, attempts = run_source(tmp_path, [(0, "Pulling file")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert attempts == 1
    assert "HEARTBEAT" in result.stdout
    attempt_log, = tmp_path.glob("transifex-pull.*")
    assert attempt_log.read_text() == "Pulling file\n"
    assert attempt_log.stat().st_mode & 0o777 == 0o600


def test_real_timeout_terminates_stalled_acquisition(tmp_path):
    timeout = shutil.which("timeout") or shutil.which("gtimeout")
    if timeout is None:
        pytest.skip("GNU timeout is required for the production shell")
    script = (ROOT / "update-translations.sh").read_text()
    functions = script[script.index("transifex_failure_is_retryable() {"):script.index("prepare_translation_source() {")]
    shell = f'''
set -euo pipefail
log() {{ :; }}
timeout() {{ {shlex.quote(timeout)} "$@"; }}
LOG_DIR={shlex.quote(str(tmp_path))}
TX_PULL_TIMEOUT_SECONDS=1
TX_PULL_CMD=(bash -c 'echo "Response error code 502"; sleep 30')
{functions}
run_transifex_pull
'''
    started = time.monotonic()
    result = subprocess.run(["bash", "-c", shell], text=True, capture_output=True, timeout=5)
    assert result.returncode == 124, result.stdout + result.stderr
    assert time.monotonic() - started < 4
    assert len(list(tmp_path.glob("transifex-pull.*"))) == 1
