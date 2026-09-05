import importlib
import os
import re
import subprocess
from pathlib import Path

# The session autouse fixture patches this module by name.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
_TRANSLATE_MODULE = importlib.import_module("localize.translate_localization_files")


REPO_ROOT = Path(__file__).resolve().parents[2]

# CodeRabbit refuses to review a pull request with more than this many files
# ("Too many files! This PR contains N files, which is X over the limit of 100").
# A batch at or above this size is published without any review, so the split
# threshold has to stay strictly below it.
CODERABBIT_MAX_REVIEWABLE_FILES = 100


def _script_default_max_files_per_pr() -> int:
    script = (REPO_ROOT / "update-translations.sh").read_text()
    match = re.search(r"^DEFAULT_MAX_FILES_PER_PR=(\d+)", script, re.MULTILINE)

    assert match is not None
    return int(match.group(1))


def test_default_max_files_per_pr_stays_under_coderabbit_review_limit():
    assert _script_default_max_files_per_pr() < CODERABBIT_MAX_REVIEWABLE_FILES


def test_publish_translation_changes_runs_under_set_u(tmp_path):
    script = (REPO_ROOT / "update-translations.sh").read_text()
    start = script.index("publish_translation_changes() {")
    end = script.index("\npublish_translation_changes\n", start)
    function_text = script[start:end]
    harness = f"""
set -euo pipefail
log() {{ :; }}
record_pipeline_event() {{ :; }}
command_exists() {{ return 1; }}
collect_changed_translation_files() {{ printf '%s\\n' "$1/messages_de.properties"; }}
stage_and_submit_batch() {{ return 0; }}
mapfile() {{
  local array_name
  if [ "${{1:-}}" = "-t" ]; then
    shift
  fi
  array_name="$1"
  eval "$array_name=()"
  local line
  while IFS= read -r line; do
    eval "$array_name+=(\\"\\$line\\")"
  done
}}
git() {{
  case "$*" in
    "config user.name "*) return 0 ;;
    "config user.email "*) return 0 ;;
    "remote") printf '%s\\n' origin ;;
    "remote get-url origin") printf '%s\\n' git@github.com:owner/repo.git ;;
    *) return 0 ;;
  esac
}}
{function_text}
APP_ROOT={str(tmp_path)!r}
TARGET_PROJECT_ROOT={str(tmp_path)!r}
ABSOLUTE_INPUT_FOLDER={str(tmp_path / "resources")!r}
INPUT_FOLDER=resources
DRY_RUN=false
MAX_FILES_PER_PR=150
TRANSLATION_BRANCH_PREFIX=translation-updates
publish_translation_changes
"""

    result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

    assert result.returncode == 0, result.stderr


def test_every_published_batch_stays_within_coderabbit_review_limit(tmp_path):
    """A run large enough to split must not emit a batch CodeRabbit will skip.

    bisq-network/bisq2#4891 shipped 150 files in one batch and was published
    with no review at all, so assert on the emitted batch sizes rather than on
    the threshold constant alone.
    """
    script = (REPO_ROOT / "update-translations.sh").read_text()
    start = script.index("publish_translation_changes() {")
    end = script.index("\npublish_translation_changes\n", start)
    function_text = script[start:end]
    # Run the script's own default/validation block rather than injecting a
    # threshold, so the batch sizes below depend on the real defaulting path.
    threshold_start = script.index("DEFAULT_MAX_FILES_PER_PR=")
    threshold_end = script.index("\nfi\n", threshold_start) + len("\nfi\n")
    threshold_text = script[threshold_start:threshold_end]
    # Three 54-locale resource groups, the shape of the 2026-07-24 run.
    total_files = 162
    harness = f"""
set -euo pipefail
log() {{ :; }}
record_pipeline_event() {{ :; }}
command_exists() {{ return 1; }}
collect_changed_translation_files() {{
  for i in $(seq 1 {total_files}); do
    printf '%s\\n' "$1/messages_$i.properties"
  done
}}
stage_and_submit_batch() {{ printf 'BATCH %s\\n' "${{#BATCH_FILES[@]}}"; return 0; }}
mapfile() {{
  local array_name
  if [ "${{1:-}}" = "-t" ]; then
    shift
  fi
  array_name="$1"
  eval "$array_name=()"
  local line
  while IFS= read -r line; do
    eval "$array_name+=(\\"\\$line\\")"
  done
}}
git() {{
  case "$*" in
    "config user.name "*) return 0 ;;
    "config user.email "*) return 0 ;;
    "remote") printf '%s\\n' origin ;;
    "remote get-url origin") printf '%s\\n' git@github.com:owner/repo.git ;;
    *) return 0 ;;
  esac
}}
{function_text}
APP_ROOT={str(tmp_path)!r}
TARGET_PROJECT_ROOT={str(tmp_path)!r}
ABSOLUTE_INPUT_FOLDER={str(tmp_path / "resources")!r}
INPUT_FOLDER=resources
DRY_RUN=false
unset MAX_FILES_PER_PR
{threshold_text}
TRANSLATION_BRANCH_PREFIX=translation-updates
publish_translation_changes
"""

    result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    batch_sizes = [
        int(line.split()[1])
        for line in result.stdout.splitlines()
        if line.startswith("BATCH ")
    ]

    assert batch_sizes, result.stdout
    assert sum(batch_sizes) == total_files
    for size in batch_sizes:
        assert size < CODERABBIT_MAX_REVIEWABLE_FILES, batch_sizes


def test_max_files_per_pr_is_validated_before_batching():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    default_index = script.index("DEFAULT_MAX_FILES_PER_PR=")
    validate_index = script.index('[[ ! "$MAX_FILES_PER_PR" =~ ^[1-9][0-9]*$ ]]')
    batch_index = script.index("stage_and_submit_batch()")

    assert default_index < validate_index < batch_index
    assert "Invalid MAX_FILES_PER_PR" in script
    assert "MAX_FILES_PER_PR=$DEFAULT_MAX_FILES_PER_PR" in script


def test_env_example_documents_max_files_per_pr_override():
    env_example = (REPO_ROOT / "docker" / ".env.example").read_text()

    match = re.search(r"^MAX_FILES_PER_PR=(\d+)", env_example, re.MULTILINE)

    assert match is not None
    # The documented override must not drift away from the script default, and
    # must not talk operators into a value CodeRabbit refuses to review.
    assert int(match.group(1)) == _script_default_max_files_per_pr()
    assert int(match.group(1)) < CODERABBIT_MAX_REVIEWABLE_FILES
    assert "150 files" not in env_example


def test_health_check_reads_github_token_from_main_or_mobile_install():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text()

    assert (
        'for env_file in "$INSTALL_ROOT/docker/.env" "$MOBILE_INSTALL_ROOT/docker/.env"'
        in script
    )
    assert 'if [ -z "$GITHUB_TOKEN" ] && [ -f "$env_file" ]; then' in script


def test_health_check_alerts_on_stale_completed_cron_runs():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text()

    assert "cron_log_files()" in script
    assert "combine_cron_logs()" in script
    assert 'gzip -cd -- "$file"' in script
    assert (
        'MAX_CRON_SUCCESS_AGE_SECONDS="${MAX_CRON_SUCCESS_AGE_SECONDS:-93600}"'
        in script
    )
    assert 'local max_success_age_sec="$3"' in script
    assert "last completed run is too old" in script
    assert (
        'check_cron_log "Main service" "$main_log" "$MAX_CRON_SUCCESS_AGE_SECONDS"'
        in script
    )
    assert (
        'check_cron_log "Mobile app service" "$mobile_log" "$MAX_CRON_SUCCESS_AGE_SECONDS"'
        in script
    )


def test_health_check_verifies_job_heartbeat_attempts():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text()

    assert "Warning: Health check ping failed" in script
    assert "did not attempt a heartbeat" in script
    assert "Sending heartbeat to health check URL" in script


def test_generated_prs_publish_translation_quality_gate_status():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "localize.translation_quality_gate" in script
    assert "localize.translation_semantic_reviewer" in script
    assert 'QUALITY_AUDIT_SCOPE="${TRANSLATION_QUALITY_AUDIT_SCOPE:-}"' in script
    assert 'if [ -n "$QUALITY_AUDIT_SCOPE" ]; then' in script
    assert 'QUALITY_GATE_CMD+=(--audit-scope "$QUALITY_AUDIT_SCOPE")' in script
    assert "translation-quality-gate" in script
    assert 'status_repo="${FORK_OWNER}/${FORK_REPO_NAME_SHORT}"' in script
    assert 'gh api "repos/$status_repo/statuses/$commit_sha"' in script
    assert 'gh api "repos/$status_repo/commits/$commit_sha/status"' in script
    assert "for verify_attempt in 1 2 3 4 5" in script
    assert 'sleep "$verify_delay"' in script
    assert "verify_delay=$((verify_delay * 2))" in script
    assert "2>/dev/null || true" in script
    assert "QUALITY_REPORT_MD" in script


def test_generated_prs_abort_when_enabled_semantic_review_fails():
    script = (REPO_ROOT / "update-translations.sh").read_text()
    stage = script[
        script.index("stage_and_submit_batch()") : script.index(
            "publish_translation_changes()"
        )
    ]

    failure = stage[stage.index('if [ "$SEMANTIC_REVIEW_EXIT" -ne 0 ]; then') :]
    assert 'log "Semantic AI review failed; refusing publication." "ERROR"' in failure
    assert "return 1" in failure[: failure.index('log "Re-staging translation files')]


def test_each_batch_resets_and_verifies_its_exact_staged_paths():
    script = (REPO_ROOT / "update-translations.sh").read_text()
    stage = script[
        script.index("stage_and_submit_batch()") : script.index(
            "publish_translation_changes()"
        )
    ]

    checkout_index = stage.index('git checkout -B "$branch"')
    reset_index = stage.index("git reset -q")
    first_stage_index = stage.index('for bf in "${BATCH_FILES[@]}"')
    exact_index = stage.index(
        "staged paths do not exactly match the current translation batch"
    )
    commit_index = stage.index('commit_staged_changes "$commit_msg"')
    assert checkout_index < reset_index < first_stage_index < exact_index < commit_index
    assert '["git", "diff", "--cached", "--name-only"' in stage


def test_fork_repo_name_short_strips_git_suffix_before_status_api():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    normalize_index = script.index('FORK_REPO_NAME_SHORT=$(echo "$origin_url"')
    submit_index = script.index('if ! stage_and_submit_batch "$BRANCH_NAME"')

    assert "origin_url=$(git remote get-url origin)" in script
    assert "s#\\.git$##" in script
    assert 'status_repo="${FORK_OWNER}/${FORK_REPO_NAME_SHORT}"' in script
    assert normalize_index < submit_index


def test_config_file_is_normalized_before_late_quality_gate_call():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    normalize_index = script.index('resolve_config_file\nlog "Using configuration file')
    quality_gate_index = script.index("localize.translation_quality_gate")

    assert normalize_index < quality_gate_index


def test_validation_summary_is_reset_before_translation_script_runs():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    reset_index = script.index("translation_validation_summary.json")
    python_index = script.index(
        'python3 -u -m localize.cli run --config "$CONFIG_FILE"'
    )

    assert reset_index < python_index
    assert '{"files":{},"pipeline_warnings":[]}' in script


def test_localize_dry_run_env_overrides_shell_dry_run_config():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    dry_run_index = script.index('DRY_RUN=$(get_config_value "dry_run" "$CONFIG_FILE")')
    override_index = script.index('case "${LOCALIZE_DRY_RUN:-}" in')
    publish_index = script.index("publish_translation_changes()")

    assert dry_run_index < override_index < publish_index
    assert "DRY_RUN=true" in script[override_index:publish_index]


def test_smoke_only_mode_runs_before_pending_pr_guard():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    smoke_index = script.index("\nrun_smoke_only_if_requested\n")
    pending_guard_index = script.index("Checking for manually-blocked PRs")

    assert "run_smoke_only_if_requested()" in script
    assert 'local app_root="${APP_ROOT:-/app}"' in script
    assert (
        '( cd "$app_root" && python3 -m localize.cli doctor --config "$CONFIG_FILE" )'
        in script
    )
    assert (
        '( cd "$app_root" && python3 -m localize.cli smoke --config "$CONFIG_FILE" )'
        in script
    )
    assert "python3 -m localize.cli doctor" in script
    assert "python3 -m localize.cli smoke" in script
    assert smoke_index < pending_guard_index


def test_script_emits_structured_pipeline_events_for_monitoring():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "record_pipeline_event()" in script
    assert 'payload="{\\"event\\":$(record_pipeline_event_field "$event")"' in script
    assert 'log "PIPELINE_EVENT $payload"' in script
    assert "record_pipeline_event_field()" in script
    assert 'record_pipeline_event "pending_pr_guard"' in script
    assert 'record_pipeline_event "source_files_detected"' in script
    assert 'record_pipeline_event "translation_files_detected"' in script
    assert 'record_pipeline_event "files_processed"' in script
    assert 'record_pipeline_event "skipped_files"' in script
    assert 'record_pipeline_event "pull_request_created"' in script


def test_semantic_remediation_changes_are_restaged_before_quality_gate():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    review_index = script.index("localize.translation_semantic_reviewer")
    restage_index = script.index("Re-staging translation files after semantic review")
    quality_gate_index = script.index("localize.translation_quality_gate")

    assert review_index < restage_index < quality_gate_index


def test_translation_source_is_read_and_defaults_to_transifex():
    """translation_source is read from config and defaults to transifex (back-compat)."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert (
        'TRANSLATION_SOURCE=$(get_config_value "translation_source" "$CONFIG_FILE")'
        in script
    )
    assert 'TRANSLATION_SOURCE="${TRANSLATION_SOURCE:-transifex}"' in script


def test_translation_source_is_normalized_and_validated():
    """translation_source is lowercased and unknown values fall back with a warning."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "normalize_translation_source()" in script
    assert "is_supported_translation_source()" in script
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert 'is_supported_translation_source "$TRANSLATION_SOURCE"' in script
    # Normalization must happen before the git-source guard is evaluated.
    norm_index = script.index("TRANSLATION_SOURCE=$(normalize_translation_source")
    guard_index = script.index('prepare_translation_source "$TRANSLATION_SOURCE"')
    assert norm_index < guard_index


def test_transifex_pull_is_skipped_when_source_is_git():
    """A git-source project must skip the Transifex pull entirely."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "prepare_translation_source()" in script
    assert '"$translation_source" == "git"' in script
    assert "using localization files already in the repository" in script
    # The guard must be evaluated before the tx pull command is constructed.
    guard_index = script.index('"$translation_source" == "git"')
    tx_pull_index = script.index("TX_PULL_CMD=(tx pull")
    assert guard_index < tx_pull_index


def test_git_source_prepares_diff_baseline_before_pipeline():
    """Git-source cron runs must diff against the last processed upstream commit."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "prepare_git_source_diff_base()" in script
    assert 'prepare_git_source_diff_base "$TRANSLATION_SOURCE"' in script
    assert 'export TRANSLATION_DIFF_BASE="$baseline_sha"' in script
    assert 'git merge-base --is-ancestor "$baseline_sha" HEAD' in script

    checkout_index = script.index('git checkout -B "${DEFAULT_BRANCH}"')
    baseline_index = script.index('prepare_git_source_diff_base "$TRANSLATION_SOURCE"')
    prepare_index = script.index('prepare_translation_source "$TRANSLATION_SOURCE"')
    python_index = script.index(
        'python3 -u -m localize.cli run --config "$CONFIG_FILE"'
    )

    assert checkout_index < baseline_index < prepare_index < python_index


def test_git_source_baseline_uses_persistent_logs_state():
    """The baseline survives container restarts by living under the logs mount."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "sanitize_state_component()" in script
    assert 'state_dir="${LOCALIZE_STATE_DIR:-$LOG_DIR/state}"' in script
    assert (
        'input_component=$(sanitize_state_component "${INPUT_FOLDER:-default}")'
        in script
    )
    assert "git-source-baseline" in script
    assert "GIT_SOURCE_BASELINE_FILE=" in script
    assert "GIT_SOURCE_CURRENT_HEAD=" in script


def test_git_source_baseline_advances_only_after_no_change_success():
    """Do not advance the baseline while a generated translation PR is pending."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "update_git_source_baseline_if_safe()" in script
    assert "TRANSLATION_CHANGES_CREATED=false" in script
    assert "TRANSLATION_CHANGES_CREATED=true" in script
    assert '"${DRY_RUN:-false}" == "true"' in script
    assert '"${TRANSLATION_CHANGES_CREATED:-false}" == "true"' in script
    assert (
        "Not advancing git-source baseline because translation changes were produced"
        in script
    )

    publish_index = script.index("\npublish_translation_changes\n")
    update_index = script.index(
        'update_git_source_baseline_if_safe "$TRANSLATION_SOURCE"'
    )
    return_branch_index = script.index("# Go back to original branch")

    assert publish_index < update_index < return_branch_index


def test_translation_source_read_before_transifex_step():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    read_index = script.index(
        'TRANSLATION_SOURCE=$(get_config_value "translation_source"'
    )
    prepare_index = script.index('prepare_translation_source "$TRANSLATION_SOURCE"')
    assert read_index < prepare_index


def test_source_adapter_is_prepared_before_python_pipeline_runs():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    prepare_index = script.index('prepare_translation_source "$TRANSLATION_SOURCE"')
    python_index = script.index(
        'python3 -u -m localize.cli run --config "$CONFIG_FILE"'
    )

    assert prepare_index < python_index


def test_publish_adapter_wraps_commit_and_pr_flow():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "publish_translation_changes()" in script
    assert "translation_file_extension_regex()" in script
    assert "translation_file_status_regex()" in script
    assert "collect_changed_translation_files()" in script
    publish_def_index = script.index("publish_translation_changes()")
    publish_call_index = script.index(
        "publish_translation_changes", publish_def_index + 1
    )
    return_branch_index = script.index("Returning to original branch")

    assert publish_def_index < publish_call_index < return_branch_index


def test_publish_adapter_preserves_both_paths_for_translation_renames():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert (
        "translation_file_status_regex() {\n    translation_file_change_regex\n}"
        in script
    )
    assert 'extension_regex="\\\\.($(translation_file_extension_regex))$"' in script
    assert 'old_path = substr(path, 1, index(path, " -> ") - 1)' in script
    assert 'new_path = substr(path, index(path, " -> ") + 4)' in script
    assert "if (old_path ~ extension_regex) print old_path" in script
    assert "if (new_path ~ extension_regex) print new_path" in script


def test_publish_adapter_supports_json_translation_files():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "translation_file_extension_regex()" in script
    assert "localization_formats" in script
    assert "file_extension" in script
    assert "json)\n            printf 'json'" in script
    assert 'java_properties|""|"null")\n            printf \'properties\'' in script


def test_publish_adapter_supports_mixed_format_profiles():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    function_body = script[
        script.index("translation_file_extension_regex() {") : script.index(
            "collect_changed_translation_files()"
        )
    ]

    assert "localization_formats" in function_body
    assert 'format_extension(profile.get("format"))' in function_body
    assert 'format_extension(profile.get("localization_format"))' in function_body
    assert "extensions.add(extension_for_format(format_id))" in function_body
    assert 'print("|".join(sorted(extensions)))' in function_body


def test_translation_file_extension_override_precedes_format_id_defaults():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    function_body = script[
        script.index("translation_file_extension_regex() {") : script.index(
            "collect_changed_translation_files()"
        )
    ]

    extension_normalize_index = function_body.index("normalize_extension")
    case_index = function_body.index("extension_for_format")

    assert extension_normalize_index < case_index
    assert "return" in function_body[extension_normalize_index:case_index]


def test_pr_body_includes_token_usage_cost_summary():
    """The per-run cost summary is surfaced in the PR description."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "token_usage_summary.json" in script
    assert "Translation cost" in script
    assert "--token-usage-summary" in script
    assert "all AI stages" in script
    assert ".stages" in script
    # The cost section must be assembled before the PR is created.
    cost_index = script.index("token_usage_summary.json")
    pr_create_index = script.index("gh pr create")
    assert cost_index < pr_create_index


def test_pr_body_includes_translation_run_summary_metrics():
    """The PR description explains candidate, model, memory, and skipped counts."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "translation_summary.json" in script
    assert "Translation run summary" in script
    assert "changed_values_count" in script
    assert "candidate_keys_count" in script
    assert "model_translation_keys_count" in script
    assert "model_translation_failed_count" in script
    assert "translation_memory_reused_count" in script
    assert "source_identical_skipped_count" in script

    summary_index = script.index("Translation run summary")
    pr_create_index = script.index("gh pr create")
    assert summary_index < pr_create_index


def test_pending_pr_gate_does_not_swallow_gh_failures():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "if ! MANUAL_BLOCK_PR=$(gh pr list" in script
    assert "if ! existing_pr_branches=$(gh pr list" in script
    assert (
        'gh pr list --state open --author "@me" --repo "$UPSTREAM_REPO_NAME" --search "in:title $BLOCKING_KEYWORD" --json number -q \'.[0].number\' || true'
        not in script
    )
    assert (
        'gh pr list --state open --author "@me" --repo "$UPSTREAM_REPO_NAME" --json headRefName -q \'.[].headRefName\' | grep'
        not in script
    )
    assert "Failed to query manually-blocked PRs" in script
    assert "Failed to query existing translation PRs" in script


def test_publish_adapter_uses_collected_translation_files_for_changes():
    script = (REPO_ROOT / "update-translations.sh").read_text()
    publish_body = script[
        script.index("publish_translation_changes() {") : script.index(
            "# Go back to original branch"
        )
    ]

    assert (
        'mapfile -t ALL_FILES < <(collect_changed_translation_files "$REL_INPUT_FOLDER")'
        in publish_body
    )
    assert "TRANSLATION_CHANGES=$(printf" in publish_body
    assert "No git-scoped translation file changes detected" in publish_body
    assert "git status --porcelain | grep -E" not in publish_body


def test_shell_helpers_are_local_run_safe_and_nonfatal():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert 'SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)' in script
    assert 'LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"' in script
    assert 'LOG_FILE="$LOG_DIR/deployment_log.log"' in script
    log_cmd_body = script[
        script.index("log_cmd() {") : script.index("record_pipeline_event()")
    ]
    assert "local status=${PIPESTATUS[0]}" in log_cmd_body
    assert "return 0" in log_cmd_body


def test_pipeline_events_use_one_key_value_per_argument():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert (
        'record_pipeline_event "source_files_detected" "count=0" "source=transifex"'
        in script
    )
    assert (
        'record_pipeline_event "source_files_detected" "count=${SOURCE_CHANGE_COUNT:-0}" "source=transifex"'
        in script
    )
    assert (
        'record_pipeline_event "pull_request_created" "url=$PR_URL" "branch=$branch" "files=${#BATCH_FILES[@]}"'
        in script
    )
    assert (
        'record_pipeline_event "translation_files_detected" "count=$TOTAL_FILES" "input_folder=$REL_INPUT_FOLDER"'
        in script
    )
    assert '"count=0 source=transifex"' not in script


def test_transifex_is_required_only_for_transifex_source_and_times_out():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    tool_loop = script[
        script.index("for tool in yq git curl jq python3") : script.index(
            "# --- Execution Lock"
        )
    ]
    assert " tx " not in tool_loop
    assert "TX_PULL_CMD=(tx pull -t -f --use-git-timestamps)" in script
    assert "TX_PULL_CMD=(tx pull -s -t -f --use-git-timestamps)" in script
    assert 'TX_PULL_TIMEOUT_SECONDS="${TX_PULL_TIMEOUT:-3600}"' in script
    assert 'timeout "$TX_PULL_TIMEOUT_SECONDS" "${TX_PULL_CMD[@]}"' in script


def test_publish_flow_preflights_gh_and_caps_pr_body():
    script = (REPO_ROOT / "update-translations.sh").read_text()
    stage = script[
        script.index("stage_and_submit_batch()") : script.index(
            "publish_translation_changes()"
        )
    ]

    preflight_index = stage.index("Cannot create PR: gh CLI or GITHUB_TOKEN missing.")
    commit_index = stage.index('if ! commit_staged_changes "$commit_msg"')
    assert preflight_index < commit_index
    assert 'PR_BODY_FILE="$report_dir/pr-body-${branch}.md"' in stage
    assert "max_chars = 60000" in stage
    assert '--body-file "$PR_BODY_FILE"' in stage
    assert "cut -c1-140" not in stage
    assert "| .[:140]" in stage


def test_publish_flow_reports_failed_batches():
    script = (REPO_ROOT / "update-translations.sh").read_text()
    publish = script[script.index("publish_translation_changes()") :]

    assert "FAILED_BATCHES=0" in publish
    assert "FAILED_BATCHES=$((FAILED_BATCHES + 1))" in publish
    assert 'if [ "$FAILED_BATCHES" -gt 0 ]; then' in publish
    assert "exit 1" in publish[publish.index('if [ "$FAILED_BATCHES" -gt 0 ]; then') :]


def test_git_operations_are_scoped_and_explicit():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert 'LOCK_DIR="${LOCALIZE_STATE_DIR:-$LOG_DIR/state}"' in script
    assert (
        'LOCK_FILE="$LOCK_DIR/translation-${UPSTREAM_REPO_NAME//\\//-}.lock"' in script
    )
    config_body = script[
        script.index("get_config_value()") : script.index(
            "prepare_translation_source()"
        )
    ]
    assert "local val" in config_body
    assert "ABSOLUTE_INPUT_FOLDER=$(echo" not in script
    assert "git config --type=bool --get commit.gpgsign" in script
    assert 'git rm --ignore-unmatch -- "$bf"' in script
    assert "Warning: failed to remove deleted translation file" in script
