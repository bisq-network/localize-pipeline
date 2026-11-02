"""Execute publication and pending-PR paths without external side effects."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run(shell, tmp_path):
    """Run real shell functions with isolated service doubles."""
    return subprocess.run(
        ["bash", "-c", shell], cwd=tmp_path, text=True, capture_output=True,
        env=dict(os.environ, APP_ROOT=str(tmp_path)),
    )


@pytest.mark.parametrize("states", [[], ["failure"], ["failure", "success"], ["success", "failure"], ["success", "success"]])
def test_published_quality_controls_final_exit_and_heartbeat(tmp_path, states):
    """Publish every batch and retain failed evidence before rejecting the run."""
    script = (ROOT / "update-translations.sh").read_text()
    functions = script[script.index("stage_and_submit_batch() {"):]
    (tmp_path / "logs").mkdir()
    (tmp_path / "resources").mkdir()
    files = [f"resources/messages_{i}.properties" for i in range(len(states))]
    for file in files:
        (tmp_path / file).write_text("key=value\n")
    plan = json.dumps({"files": files, "skipped_count": 0})
    shell = r'''
set -euo pipefail
log() { printf '%s\n' "$*"; }
record_pipeline_event() { :; }
command_exists() { return 0; }
commit_staged_changes() { return 0; }
send_heartbeat_if_configured() { echo HEARTBEAT; }
update_git_source_baseline_if_safe() { echo BASELINE; }
collect_changed_translation_files() { if [ "${#FILES[@]}" -eq 0 ]; then return 0; fi; printf '%s\n' "${FILES[@]}"; }
mapfile() {
  shift
  local array_name="$1" line
  eval "$array_name=()"
  while IFS= read -r line; do eval "$array_name+=(\"\$line\")"; done
}
git() {
  case "$*" in
    remote) echo origin;;
    "remote get-url origin") echo https://github.com/owner/fork.git;;
    "rev-parse HEAD") echo deadbeef;;
  esac
}
python3() {
  if [ "${2:-}" = localize.translation_publication ]; then
    printf '%s' "$PLAN"
  elif [ "${2:-}" = localize.translation_quality_gate ]; then
    local output='' branch='' argument
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --output-json ]; then output="$2"; break; fi
      shift
    done
    local index=$(cat "$APP_ROOT/count")
    local state=${STATES[$index]}
    echo "$state" > "$APP_ROOT/state"
    echo $((index+1)) > "$APP_ROOT/count"
    if [ "$state" = failure ]; then
      echo '{"blocking":true,"status_state":"failure"}' > "$output"
      return 1
    fi
    echo '{"blocking":false,"status_state":"success"}' > "$output"
  fi
}
gh() {
  case "$*" in
    "pr create "*) echo PUBLISHED >> "$APP_ROOT/events"; echo https://github.com/upstream/repo/pull/42;;
    "api "*"/statuses/"*) echo "STATUS $(cat "$APP_ROOT/state")" >> "$APP_ROOT/events";;
    "api "*"/commits/"*) cat "$APP_ROOT/state";;
  esac
}
GITHUB_TOKEN=test
REMOTE=origin
DEFAULT_BRANCH=main
ORIGINAL_BRANCH=main
TARGET_PROJECT_ROOT="$APP_ROOT"
ABSOLUTE_INPUT_FOLDER="$APP_ROOT/resources"
CONFIG_FILE="$APP_ROOT/config.yaml"
UPSTREAM_REPO_NAME=upstream/repo
FORK_REPO_NAME=owner/fork
TARGET_BRANCH_FOR_PR=main
TRANSLATION_BRANCH_PREFIX=translation-updates
TRANSLATION_SOURCE=git
DRY_RUN=false
MAX_FILES_PER_PR=1
echo 0 > "$APP_ROOT/count"
'''
    shell += f"\nPLAN='{plan}'\nSTATES=({' '.join(states)})\nFILES=({' '.join(files)})\n"
    if not states:
        # macOS Bash 3 treats declared empty arrays as unset under -u. The
        # production Bash supports them; exercise no-change control flow here.
        shell = shell.replace("set -euo pipefail", "set -eo pipefail", 1)
    result = _run(shell + functions, tmp_path)
    events = (tmp_path / "events").read_text().splitlines() if states else []
    assert events.count("PUBLISHED") == len(states), result.stdout + result.stderr
    assert [event for event in events if event.startswith("STATUS")] == [f"STATUS {state}" for state in states]
    blocked = "failure" in states
    assert result.returncode == (1 if blocked else 0), result.stdout + result.stderr
    assert ("HEARTBEAT" in result.stdout) is not blocked
    assert ("BASELINE" in result.stdout) is not blocked
    assert len(list((tmp_path / "logs").glob("translation_quality_report-*.json"))) == len(states)


@pytest.mark.parametrize("states, expected", [(["SUCCESS"], 0), (["FAILURE"], 1), (["SUCCESS", "FAILURE"], 1), (["PENDING"], 1), (["ERROR"], 1), ([None], 1), (["HEAD_CHANGED"], 1), (["WRONG_REPO"], 1), (["API_ERROR"], 1)])
def test_pending_translation_quality_must_be_successful(tmp_path, states, expected):
    """An existing blocked or unverified PR cannot clear the quality alert."""
    script = (ROOT / "update-translations.sh").read_text()
    helper = script[script.index("check_and_exit_if_blocked() {"):script.index("# Check for required tools.")]
    guard = script[script.index("# --- Pull Request Gate ---"):script.index("# --- Git Repository and Transifex Configuration Validation ---")]
    prs = [{"number": i + 1, "headRefOid": f"{i + 1:040x}", "headRepository": {"nameWithOwner": "owner/fork"}, "headRefName": f"translation-updates-{i}", "statusCheckRollup": [] if state is None else [{"context": "translation-quality-gate", "state": state}]} for i, state in enumerate(states)]
    shell = r'''
set -euo pipefail
log() { echo "$*"; }
record_pipeline_event() { :; }
send_heartbeat_if_configured() { echo HEARTBEAT; }
command_exists() { return 0; }
gh() {
  case "$*" in
    *"--search"*) return 0;;
    "api "*)
      if [ "$SCENARIO" = API_ERROR ]; then return 1; fi
      local head="${2%/status}"
      head="${head##*/}"
      printf '%s' "$PRS" | jq -r --arg head "$head" '[.[] | select(.headRefOid == $head) | .statusCheckRollup[]? | select(.context == "translation-quality-gate")][0].state // "" | ascii_downcase';;
    *)
      if [ "$SCENARIO" = HEAD_CHANGED ] && [ -f "$APP_ROOT/listed" ]; then
        printf '%s' "$PRS" | jq '.[0].headRefOid="ffffffffffffffffffffffffffffffffffffffff"'
      else printf '%s' "$PRS"; fi
      touch "$APP_ROOT/listed";;
  esac
}
UPSTREAM_REPO_NAME=upstream/repo
FORK_REPO_NAME=owner/fork
GITHUB_TOKEN=test
TRANSLATION_BRANCH_PREFIX=translation-updates
'''
    scenario = states[0] or "MISSING"
    if scenario in {"HEAD_CHANGED", "WRONG_REPO", "API_ERROR"}:
        prs[0]["statusCheckRollup"][0]["state"] = "SUCCESS"
    if scenario == "WRONG_REPO":
        prs[0]["headRepository"]["nameWithOwner"] = "untrusted/fork"
    result = _run(shell + f"\nSCENARIO={scenario}\nPRS='{json.dumps(prs)}'\n" + helper + guard, tmp_path)
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("HEARTBEAT" in result.stdout) == (expected == 0)


@pytest.mark.parametrize("mode", ["manual", "unavailable", "malformed", "truncated"])
def test_unverified_pending_guard_withholds_success(tmp_path, mode):
    """Manual blocks and missing GitHub evidence never emit healthy receipts."""
    script = (ROOT / "update-translations.sh").read_text()
    helper = script[script.index("check_and_exit_if_blocked() {"):script.index("# Check for required tools.")]
    guard = script[script.index("# --- Pull Request Gate ---"):script.index("# --- Git Repository and Transifex Configuration Validation ---")]
    shell = r'''
set -euo pipefail
log() { echo "$*"; }
record_pipeline_event() { :; }
send_heartbeat_if_configured() { echo HEARTBEAT; }
command_exists() { return 0; }
gh() {
  case "$*" in
    *"--search"*) if [ "$MODE" = manual ]; then echo 42; fi; return 0;;
    *)
      if [ "$MODE" = unavailable ]; then return 1; fi
      if [ "$MODE" = truncated ]; then
        jq -n '[range(100) | {headRefName:"other"}]'
      else echo '{bad'; fi;;
  esac
}
UPSTREAM_REPO_NAME=upstream/repo
FORK_REPO_NAME=owner/fork
GITHUB_TOKEN=test
TRANSLATION_BRANCH_PREFIX=translation-updates
'''
    result = _run(shell + f"\nMODE={mode}\n" + helper + guard, tmp_path)
    assert result.returncode == 1
    assert "HEARTBEAT" not in result.stdout
