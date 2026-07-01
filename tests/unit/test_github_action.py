"""Static structural tests for the drop-in GitHub Action (action.yml)."""
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTION = PROJECT_ROOT / "action.yml"


@pytest.fixture(scope="module")
def action():
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def test_action_exists_and_is_composite(action):
    assert ACTION.exists()
    assert action["runs"]["using"] == "composite"


def test_action_description_is_marketplace_publishable(action):
    assert len(action["description"]) < 125


def test_supports_byo_key_and_local_endpoint(action):
    inputs = action["inputs"]
    assert "openai-api-key" in inputs
    assert "api-base-url" in inputs  # Ollama / OpenAI-compatible endpoint
    # The local endpoint must be optional (empty default) so no key is required.
    assert inputs["api-base-url"]["default"] == ""


def test_uses_github_token_not_ssh_deploy_key(action):
    inputs = action["inputs"]
    assert inputs["github-token"]["default"] == "${{ github.token }}"
    rendered = ACTION.read_text(encoding="utf-8")
    assert "deploy_key" not in rendered and "id_ed25519" not in rendered


def test_translate_step_wires_provider_and_process_all_env(action):
    steps = action["runs"]["steps"]
    translate = next(s for s in steps if s["name"] == "Translate changed strings")
    env = translate["env"]
    assert "OPENAI_BASE_URL" not in env
    assert "REVIEW_MODEL_NAME" not in env
    assert env["LOCALIZE_API_BASE_URL_INPUT"] == "${{ inputs.api-base-url }}"
    assert env["LOCALIZE_REVIEW_MODEL_INPUT"] == "${{ inputs.review-model }}"
    assert env["PROCESS_ALL_FILES"] == "${{ inputs.process-all-files }}"
    assert env["LOCALIZE_DRY_RUN"] == "${{ inputs.dry-run }}"
    assert env["LOCALIZE_PLUGIN_MODULES"] == "${{ inputs.plugin-modules }}"
    assert 'unset OPENAI_BASE_URL' in translate["run"]
    assert 'unset REVIEW_MODEL_NAME' in translate["run"]
    assert 'python -m localize.cli run --config "$TRANSLATOR_CONFIG_FILE"' in translate["run"]


def test_action_runs_preflight_check_before_translation(action):
    steps = action["runs"]["steps"]
    preflight_index = next(i for i, step in enumerate(steps) if step["name"] == "Check localization setup")
    translate_index = next(i for i, step in enumerate(steps) if step["name"] == "Translate changed strings")
    assert preflight_index < translate_index
    assert "OPENAI_BASE_URL" not in steps[preflight_index]["env"]
    assert "REVIEW_MODEL_NAME" not in steps[preflight_index]["env"]
    assert steps[preflight_index]["env"]["LOCALIZE_API_BASE_URL_INPUT"] == "${{ inputs.api-base-url }}"
    assert steps[preflight_index]["env"]["LOCALIZE_REVIEW_MODEL_INPUT"] == "${{ inputs.review-model }}"
    assert steps[preflight_index]["env"]["LOCALIZE_PLUGIN_MODULES"] == "${{ inputs.plugin-modules }}"
    assert 'unset OPENAI_BASE_URL' in steps[preflight_index]["run"]
    assert 'unset REVIEW_MODEL_NAME' in steps[preflight_index]["run"]
    assert 'python -m localize.cli check --config "$TRANSLATOR_CONFIG_FILE"' in steps[preflight_index]["run"]


def test_action_has_first_class_plugin_install_and_module_inputs(action):
    inputs = action["inputs"]
    assert inputs["plugin-modules"]["default"] == ""
    assert inputs["plugin-install-command"]["default"] == ""

    steps = action["runs"]["steps"]
    dependency_index = next(i for i, step in enumerate(steps) if step["name"] == "Install pipeline dependencies")
    plugin_index = next(i for i, step in enumerate(steps) if step["name"] == "Install plugin dependencies")
    preflight_index = next(i for i, step in enumerate(steps) if step["name"] == "Check localization setup")

    assert dependency_index < plugin_index < preflight_index
    plugin_step = steps[plugin_index]
    assert "plugin-install-command" in plugin_step["if"]
    assert plugin_step["env"]["PLUGIN_INSTALL_COMMAND"] == "${{ inputs.plugin-install-command }}"
    assert 'bash -lc "$PLUGIN_INSTALL_COMMAND"' in plugin_step["run"]


def test_incremental_by_default_via_diff_base(action):
    """The Action detects changes against a base ref (not a full re-scan) by default."""
    inputs = action["inputs"]
    assert "diff-base" in inputs
    assert inputs["diff-base"]["default"] == "${{ github.event.before }}"
    # A full re-scan must be opt-in, not the default.
    assert inputs["process-all-files"]["default"] == "false"
    assert inputs["dry-run"]["default"] == "false"
    # The diff base is wired through to the pipeline env var.
    rendered = ACTION.read_text(encoding="utf-8")
    assert "TRANSLATION_DIFF_BASE" in rendered


def test_no_user_input_interpolated_into_run_scripts(action):
    """Guard against script injection: inputs must reach run: via env, not ${{ }}."""
    for step in action["runs"]["steps"]:
        run = step.get("run")
        if run:
            assert "${{ inputs." not in run, f"step '{step.get('name')}' interpolates an input into run:"


def test_opens_pr_with_gh_cli(action):
    rendered = ACTION.read_text(encoding="utf-8")
    assert "gh pr create" in rendered
    # The PR step is gated on the open-pr input.
    steps = action["runs"]["steps"]
    assert any("open-pr" in str(s.get("if", "")) for s in steps)


def test_open_pr_step_supports_optional_ssh_commit_signing(action):
    inputs = action["inputs"]
    assert inputs["git-user-name"]["default"] == "github-actions[bot]"
    assert inputs["git-user-email"]["default"] == "41898282+github-actions[bot]@users.noreply.github.com"
    assert inputs["commit-signing-method"]["default"] == "none"
    assert inputs["commit-signing-key"]["default"] == ""

    pr_step = next(step for step in action["runs"]["steps"] if step["name"] == "Open pull request")
    env = pr_step["env"]
    assert env["GIT_USER_NAME"] == "${{ inputs.git-user-name }}"
    assert env["GIT_USER_EMAIL"] == "${{ inputs.git-user-email }}"
    assert env["COMMIT_SIGNING_METHOD"] == "${{ inputs.commit-signing-method }}"
    assert env["COMMIT_SIGNING_KEY"] == "${{ inputs.commit-signing-key }}"

    run = pr_step["run"]
    assert 'case "${COMMIT_SIGNING_METHOD:-none}" in' in run
    assert "ssh-keygen -y -f \"$signing_key_file\"" in run
    assert "git config gpg.format ssh" in run
    assert "git config user.signingkey \"$signing_key_file\"" in run
    assert "git config commit.gpgsign true" in run
    assert "commit_args=(-S -m \"$COMMIT_MSG\")" in run
    assert "trap cleanup_signing_key EXIT" in run
    assert "git config user.name \"$GIT_USER_NAME\"" in run
    assert "git config user.email \"$GIT_USER_EMAIL\"" in run


def test_open_pr_step_stages_localization_changes_without_archives(action):
    pr_step = next(step for step in action["runs"]["steps"] if step["name"] == "Open pull request")
    run = pr_step["run"]
    assert "target_project_root" in run
    assert "input_folder" in run
    assert "stage_roots_file" in run
    assert "git reset -q" in run
    assert 'git add -A -- "$stage_root"' in run
    assert '":(exclude)$stage_root/archive/**"' in run
    assert '":(exclude,glob)$stage_root/**/archive/**"' in run
    assert '":(exclude,glob)**/archive/**"' in run
    assert "No stageable localization changes after excluding archive folders" in run


def test_generated_pr_uses_summary_body_and_uploads_artifacts(action):
    pr_step = next(step for step in action["runs"]["steps"] if step["name"] == "Open pull request")
    run = pr_step["run"]
    assert "translation_summary.json" in run
    assert "translation_validation_summary.json" in run
    assert "token_usage_summary.json" in run
    assert "body_file=" in run
    assert "gh pr create --head \"$branch\" --title \"$PR_TITLE\" --body-file \"$body_file\"" in run
    assert "gh pr edit \"$branch\" --title \"$PR_TITLE\" --body-file \"$body_file\"" in run

    upload = next(step for step in action["runs"]["steps"] if step["name"] == "Upload run summaries")
    assert upload["if"] == "${{ always() }}"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["name"] == "localize-pipeline-summaries"
    paths = upload["with"]["path"]
    assert "translation_summary.json" in paths
    assert "translation_validation_summary.json" in paths
    assert "token_usage_summary.json" in paths
    assert "skipped_files_report.log" in paths
    assert upload["with"]["if-no-files-found"] == "ignore"
