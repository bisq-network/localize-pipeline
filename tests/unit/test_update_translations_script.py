import importlib
import os
import re
from pathlib import Path

# The session autouse fixture patches this module by name.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
_TRANSLATE_MODULE = importlib.import_module("src.translate_localization_files")


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_max_files_per_pr_matches_coderabbit_review_limit():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    match = re.search(r"^DEFAULT_MAX_FILES_PER_PR=(\d+)", script, re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) == 150


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

    assert "MAX_FILES_PER_PR=150" in env_example


def test_generated_prs_publish_translation_quality_gate_status():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "src.translation_quality_gate" in script
    assert "src.translation_semantic_reviewer" in script
    assert 'QUALITY_AUDIT_SCOPE="${TRANSLATION_QUALITY_AUDIT_SCOPE:-changed}"' in script
    assert '--audit-scope "$QUALITY_AUDIT_SCOPE"' in script
    assert "translation-quality-gate" in script
    assert 'status_repo="${FORK_OWNER}/${FORK_REPO_NAME_SHORT}"' in script
    assert 'gh api "repos/$status_repo/statuses/$commit_sha"' in script
    assert 'gh api "repos/$status_repo/commits/$commit_sha/status"' in script
    assert "for verify_attempt in 1 2 3 4 5" in script
    assert 'sleep "$verify_delay"' in script
    assert 'verify_delay=$((verify_delay * 2))' in script
    assert "2>/dev/null || true" in script
    assert "QUALITY_REPORT_MD" in script


def test_fork_repo_name_short_strips_git_suffix_before_status_api():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    normalize_index = script.index('FORK_REPO_NAME_SHORT=$(echo "$origin_url"')
    submit_index = script.index('if ! stage_and_submit_batch "$BRANCH_NAME"')

    assert "origin_url=$(git remote get-url origin)" in script
    assert 's#\\.git$##' in script
    assert 'status_repo="${FORK_OWNER}/${FORK_REPO_NAME_SHORT}"' in script
    assert normalize_index < submit_index


def test_config_file_is_normalized_before_late_quality_gate_call():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    normalize_index = script.index("CONFIG_FILE=$(cd")
    quality_gate_index = script.index("src.translation_quality_gate")

    assert normalize_index < quality_gate_index


def test_validation_summary_is_reset_before_translation_script_runs():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    reset_index = script.index("translation_validation_summary.json")
    python_index = script.index("python3 -u -m src.translate_localization_files")

    assert reset_index < python_index
    assert '{"files":{},"pipeline_warnings":[]}' in script


def test_translation_source_is_read_and_defaults_to_transifex():
    """translation_source is read from config and defaults to transifex (back-compat)."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert 'TRANSLATION_SOURCE=$(get_config_value "translation_source" "$CONFIG_FILE")' in script
    assert 'TRANSLATION_SOURCE="${TRANSLATION_SOURCE:-transifex}"' in script


def test_translation_source_is_normalized_and_validated():
    """translation_source is lowercased and unknown values fall back with a warning."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "normalize_translation_source()" in script
    assert "is_supported_translation_source()" in script
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert 'is_supported_translation_source "$TRANSLATION_SOURCE"' in script
    # Normalization must happen before the git-source guard is evaluated.
    norm_index = script.index('TRANSLATION_SOURCE=$(normalize_translation_source')
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
    tx_pull_index = script.index('TX_PULL_CMD="tx pull')
    assert guard_index < tx_pull_index


def test_translation_source_read_before_transifex_step():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    read_index = script.index('TRANSLATION_SOURCE=$(get_config_value "translation_source"')
    prepare_index = script.index('prepare_translation_source "$TRANSLATION_SOURCE"')
    assert read_index < prepare_index


def test_source_adapter_is_prepared_before_python_pipeline_runs():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    prepare_index = script.index('prepare_translation_source "$TRANSLATION_SOURCE"')
    python_index = script.index("python3 -u -m src.translate_localization_files")

    assert prepare_index < python_index


def test_publish_adapter_wraps_commit_and_pr_flow():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "publish_translation_changes()" in script
    assert "translation_file_extension_regex()" in script
    assert "translation_file_status_regex()" in script
    assert "collect_changed_translation_files()" in script
    publish_def_index = script.index("publish_translation_changes()")
    publish_call_index = script.index("publish_translation_changes", publish_def_index + 1)
    return_branch_index = script.index("Returning to original branch")

    assert publish_def_index < publish_call_index < return_branch_index


def test_publish_adapter_preserves_both_paths_for_translation_renames():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "translation_file_status_regex() {\n    translation_file_change_regex\n}" in script
    assert 'extension_regex="\\\\.($(translation_file_extension_regex))$"' in script
    assert 'old_path = substr(path, 1, index(path, " -> ") - 1)' in script
    assert 'new_path = substr(path, index(path, " -> ") + 4)' in script
    assert "if (old_path ~ extension_regex) print old_path" in script
    assert "if (new_path ~ extension_regex) print new_path" in script


def test_publish_adapter_supports_json_translation_files():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "translation_file_extension_regex()" in script
    assert "format_id=$(yq -r '(.localization_format.id // .localization_format // \"java_properties\")'" in script
    assert "extension=$(yq -r '(.localization_format.file_extension // \"\")'" in script
    assert "json)\n            printf 'json'" in script
    assert "java_properties|\"\"|\"null\")\n            printf 'properties'" in script


def test_translation_file_extension_override_precedes_format_id_defaults():
    script = (REPO_ROOT / "update-translations.sh").read_text()

    function_body = script[
        script.index("translation_file_extension_regex() {"):
        script.index("collect_changed_translation_files()")
    ]

    extension_normalize_index = function_body.index('extension="${extension#.}"')
    case_index = function_body.index('case "$format_id" in')

    assert extension_normalize_index < case_index
    assert "return" in function_body[extension_normalize_index:case_index]


def test_pr_body_includes_token_usage_cost_summary():
    """The per-run cost summary is surfaced in the PR description."""
    script = (REPO_ROOT / "update-translations.sh").read_text()

    assert "token_usage_summary.json" in script
    assert "Translation cost" in script
    # The cost section must be assembled before the PR is created.
    cost_index = script.index("token_usage_summary.json")
    pr_create_index = script.index("gh pr create")
    assert cost_index < pr_create_index
