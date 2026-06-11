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
