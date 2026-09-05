import os
from pathlib import Path
from shlex import quote
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(command, *, cwd, env=None, check=True):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def test_failed_batch_cannot_leak_into_renamed_translation_batch(tmp_path):
    """A failed review must not contaminate a later renamed-file commit."""
    case_root = tmp_path / "shell $HOME ' workspace"
    case_root.mkdir()
    remote = case_root / "remote.git"
    repo = case_root / "target"
    logs = case_root / "app" / "logs"
    config = case_root / "config.yaml"
    logs.mkdir(parents=True)

    _run(["git", "init", "-q", "--bare", str(remote)], cwd=tmp_path)
    _run(["git", "clone", "-q", str(remote), str(repo)], cwd=tmp_path)
    _run(["git", "config", "user.name", "Batch Test"], cwd=repo)
    _run(["git", "config", "user.email", "batch@example.invalid"], cwd=repo)
    _run(["git", "config", "commit.gpgSign", "false"], cwd=repo)
    _run(["git", "switch", "-q", "-c", "main"], cwd=repo)

    input_folder = repo / "l10n"
    input_folder.mkdir()
    first = input_folder / "first.properties"
    second = input_folder / "second.properties"
    first.write_text("key=original first\n", encoding="utf-8")
    second.write_text("key=original second\n", encoding="utf-8")
    _run(["git", "add", "l10n"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "Seed translations"], cwd=repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], cwd=repo)

    first.write_text("key=translated first\n", encoding="utf-8")
    renamed = input_folder / "second-renamed.properties"
    second.rename(renamed)

    script = (PROJECT_ROOT / "update-translations.sh").read_text(encoding="utf-8")
    functions = script[
        script.index("commit_staged_changes() {") : script.index(
            "publish_translation_changes() {"
        )
    ]
    harness = f"""
set -euo pipefail
log() {{ :; }}
record_pipeline_event() {{ :; }}
command_exists() {{ return 0; }}
gh() {{
    if [ "${{1:-}} ${{2:-}}" = "pr create" ]; then
        printf '%s\n' 'https://example.invalid/pull/2'
    elif [ "${{1:-}}" = "api" ] && [[ "${{2:-}}" == */commits/*/status ]]; then
        printf '%s\n' 'success'
    fi
}}
python3() {{
    if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "localize.translation_semantic_reviewer" ]; then
        [ "$branch" != "batch-1" ]
        return
    fi
    if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "localize.translation_quality_gate" ]; then
        local output_json=''
        shift 2
        while [ "$#" -gt 0 ]; do
            if [ "$1" = "--output-json" ]; then
                output_json="$2"
                break
            fi
            shift
        done
        printf '%s\n' '{{"blocking":false,"status_state":"success"}}' > "$output_json"
        return 0
    fi
    command "$PYTHON_BIN" "$@"
}}
{functions}
cd {quote(str(repo))}
APP_ROOT={quote(str(logs.parent))}
TARGET_PROJECT_ROOT={quote(str(repo))}
ABSOLUTE_INPUT_FOLDER={quote(str(input_folder))}
CONFIG_FILE={quote(str(config))}
REMOTE=origin
DEFAULT_BRANCH=main
FORK_OWNER=test-owner
FORK_REPO_NAME_SHORT=test-repo
FORK_REPO_NAME=test-owner/test-repo
UPSTREAM_REPO_NAME=upstream/test-repo
TARGET_BRANCH_FOR_PR=main
GITHUB_TOKEN=test-token

BATCH_FILES=(l10n/first.properties)
if stage_and_submit_batch batch-1 'First batch' 'First batch'; then
    printf '%s\n' 'first batch unexpectedly succeeded' >&2
    exit 20
fi
test "$(git diff --cached --name-only)" = 'l10n/first.properties'

BATCH_FILES=(l10n/second.properties l10n/second-renamed.properties)
stage_and_submit_batch batch-2 'Second batch' 'Second batch'
actual_paths="$(git diff --name-only --no-renames origin/main...HEAD | sort)"
expected_paths="$(printf '%s\n' l10n/second.properties l10n/second-renamed.properties | sort)"
test "$actual_paths" = "$expected_paths"
test "$(git diff --cached --name-only)" = ''
test "$(git diff --name-only)" = 'l10n/first.properties'
"""
    env = os.environ.copy()
    env["PYTHON_BIN"] = sys.executable

    result = _run(["bash", "-c", harness], cwd=repo, env=env, check=False)

    assert result.returncode == 0, result.stderr or result.stdout
