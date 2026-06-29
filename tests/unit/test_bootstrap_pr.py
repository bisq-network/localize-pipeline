import subprocess
from pathlib import Path

import pytest
import yaml

from localize.bootstrap_pr import BootstrapPrOptions, create_bootstrap_pr


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    resources = repo / "i18n"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello\n", encoding="utf-8")
    (resources / "messages_de.properties").write_text("hello=Hallo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Add localization files")


def test_bootstrap_pr_creates_onboarding_branch_commit_and_files(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)

    result = create_bootstrap_pr(
        BootstrapPrOptions(
            target_project_root=repo,
            branch_name="localize/onboarding",
            action_ref="v0.1.0",
            push=False,
            open_pr=False,
        )
    )

    assert result.branch_name == "localize/onboarding"
    assert _git(repo, "branch", "--show-current") == "localize/onboarding"
    assert _git(repo, "log", "-1", "--format=%s") == "Add Localize Pipeline onboarding"
    assert result.created_files == (
        "config.yaml",
        "glossary.json",
        ".github/workflows/translate.yml",
        "docs/localize-pipeline.md",
    )

    config = yaml.safe_load((repo / "config.yaml").read_text(encoding="utf-8"))
    assert config["target_project_root"] == "."
    assert config["input_folder"] == "i18n"
    assert config["localization_format"] == "java_properties"
    assert config["dry_run"] is True
    assert config["supported_locales"] == [{"code": "de", "name": "German"}]

    workflow = (repo / ".github/workflows/translate.yml").read_text(encoding="utf-8")
    assert "bisq-network/localize-pipeline@v0.1.0" in workflow
    assert "dry-run: true" in workflow

    glossary = yaml.safe_load((repo / "glossary.json").read_text(encoding="utf-8"))
    assert isinstance(glossary, dict)

    guide = (repo / "docs/localize-pipeline.md").read_text(encoding="utf-8")
    assert "./venv/bin/localize check --config config.yaml" in guide
    assert "./venv/bin/localize run --dry-run --config config.yaml" in guide
    assert "\nlocalize check --config config.yaml" not in guide
    assert "dry-run: true" in guide
    assert "OPENAI_API_KEY" in guide
    assert "process-all-files: true" in guide


def test_bootstrap_pr_refuses_dirty_repo(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)
    (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="working tree is not clean"):
        create_bootstrap_pr(BootstrapPrOptions(target_project_root=repo))


def test_bootstrap_pr_refuses_to_overwrite_existing_files_without_flag(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)
    (repo / "config.yaml").write_text("dry_run: true\n", encoding="utf-8")
    _git(repo, "add", "config.yaml")
    _git(repo, "commit", "-m", "Add config")

    with pytest.raises(FileExistsError, match="config.yaml"):
        create_bootstrap_pr(BootstrapPrOptions(target_project_root=repo))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("config_path", "../config.yaml"),
        ("workflow_path", "/tmp/translate.yml"),
        ("input_folder", "../i18n"),
        ("input_folder", "/tmp/i18n"),
    ],
)
def test_bootstrap_pr_rejects_paths_that_escape_repo(tmp_path, field, value):
    repo = tmp_path / "target"
    _init_repo(repo)
    options = {"target_project_root": repo, field: value}

    with pytest.raises(ValueError, match="inside the target repository"):
        create_bootstrap_pr(BootstrapPrOptions(**options))


def test_bootstrap_pr_refuses_to_reset_existing_branch_without_flag(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)
    _git(repo, "checkout", "-b", "localize/onboarding")
    (repo / "branch-only.txt").write_text("keep me\n", encoding="utf-8")
    _git(repo, "add", "branch-only.txt")
    _git(repo, "commit", "-m", "Keep branch work")
    _git(repo, "checkout", "main")

    with pytest.raises(RuntimeError, match="already exists"):
        create_bootstrap_pr(BootstrapPrOptions(target_project_root=repo))


def test_bootstrap_pr_uses_custom_config_path_in_workflow(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)

    create_bootstrap_pr(
        BootstrapPrOptions(
            target_project_root=repo,
            config_path=".localize/config.yaml",
            branch_name="localize/custom-config",
        )
    )

    workflow = (repo / ".github/workflows/translate.yml").read_text(encoding="utf-8")
    assert "config-file: .localize/config.yaml" in workflow


def test_bootstrap_pr_generates_plugin_aware_workflow_and_guide(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)

    create_bootstrap_pr(
        BootstrapPrOptions(
            target_project_root=repo,
            branch_name="localize/plugin-onboarding",
            plugin_modules=("target_repo.localize_adapter",),
            plugin_install_command="python -m pip install .",
        )
    )

    workflow = (repo / ".github/workflows/translate.yml").read_text(encoding="utf-8")
    assert "plugin-modules: target_repo.localize_adapter" in workflow
    assert "plugin-install-command: |" in workflow
    assert "python -m pip install ." in workflow

    guide = (repo / "docs/localize-pipeline.md").read_text(encoding="utf-8")
    assert "target_repo.localize_adapter" in guide
    assert "python -m pip install ." in guide


def test_bootstrap_pr_threads_base_branch_into_workflow(tmp_path):
    repo = tmp_path / "target"
    _init_repo(repo)
    _git(repo, "checkout", "-b", "release")
    _git(repo, "checkout", "main")

    create_bootstrap_pr(
        BootstrapPrOptions(
            target_project_root=repo,
            branch_name="localize/release-onboarding",
            base_branch="release",
        )
    )

    workflow = (repo / ".github/workflows/translate.yml").read_text(encoding="utf-8")
    assert "branches: [release]" in workflow
