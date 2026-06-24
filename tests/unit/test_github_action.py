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
    translate = next(s for s in steps if s.get("env", {}).get("OPENAI_BASE_URL") is not None)
    env = translate["env"]
    assert env["OPENAI_BASE_URL"] == "${{ inputs.api-base-url }}"
    assert env["PROCESS_ALL_FILES"] == "${{ inputs.process-all-files }}"
    assert "src.translate_localization_files" in translate["run"]


def test_incremental_by_default_via_diff_base(action):
    """The Action detects changes against a base ref (not a full re-scan) by default."""
    inputs = action["inputs"]
    assert "diff-base" in inputs
    assert inputs["diff-base"]["default"] == "${{ github.event.before }}"
    # A full re-scan must be opt-in, not the default.
    assert inputs["process-all-files"]["default"] == "false"
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
