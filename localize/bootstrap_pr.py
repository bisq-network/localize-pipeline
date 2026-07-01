"""Create a self-service onboarding branch for a target repository."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from localize.init_config import (
    build_config,
    detect_locales,
    detect_project_layout,
    render_config,
)


@dataclass(frozen=True)
class BootstrapPrOptions:
    """Options for generating a Localize Pipeline onboarding branch."""

    target_project_root: Path | str = Path(".")
    input_folder: Optional[str] = None
    localization_format: str = "java_properties"
    localization_layout: str = "suffix"
    source_locale: str = "en"
    config_path: str = "config.yaml"
    glossary_path: str = "glossary.json"
    workflow_path: str = ".github/workflows/translate.yml"
    branch_name: str = "localize/onboarding"
    base_branch: Optional[str] = None
    action_ref: str = "v0.1.3"
    commit_message: str = "Add Localize Pipeline onboarding"
    pr_title: str = "Add Localize Pipeline onboarding"
    overwrite: bool = False
    reset_branch: bool = False
    onboarding_guide_path: Optional[str] = "docs/localize-pipeline.md"
    plugin_modules: tuple[str, ...] = ()
    plugin_install_command: Optional[str] = None
    push: bool = False
    open_pr: bool = False


@dataclass(frozen=True)
class BootstrapPrResult:
    """Summary of generated onboarding work."""

    branch_name: str
    created_files: tuple[str, ...]
    commit_sha: str
    pushed: bool = False
    opened_pr: bool = False


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        check=check,
        text=True,
        capture_output=True,
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(("git", *args), cwd=repo, check=check)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _assert_git_repo(repo: Path) -> None:
    result = _git(repo, "rev-parse", "--show-toplevel")
    if Path(result.stdout.strip()).resolve() != repo.resolve():
        raise RuntimeError(f"target_project_root must be the git repository root: {repo}")


def _assert_clean_worktree(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain").stdout.strip()
    if status:
        raise RuntimeError("target repository working tree is not clean.")


def _repo_relative_path(repo: Path, raw_path: str, label: str) -> str:
    """Return a normalized repo-relative path, rejecting escapes."""
    path = Path(raw_path)
    if path.is_absolute():
        raise ValueError(f"{label} must be inside the target repository.")
    repo_root = repo.resolve()
    resolved = (repo_root / path).resolve()
    try:
        relative_path = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the target repository.") from exc
    return relative_path.as_posix()


def _branch_exists(repo: Path, branch_name: str) -> bool:
    result = _git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch_name}",
        check=False,
    )
    return result.returncode == 0


def _checkout_onboarding_branch(repo: Path, options: BootstrapPrOptions, base_branch: str) -> None:
    _git(repo, "checkout", base_branch)
    if _branch_exists(repo, options.branch_name):
        if not options.reset_branch:
            raise RuntimeError(
                f"Branch '{options.branch_name}' already exists. "
                "Re-run with --reset-branch to replace it."
            )
        _git(repo, "checkout", "-B", options.branch_name)
        return
    _git(repo, "checkout", "-b", options.branch_name)


def _assert_can_write(repo: Path, relative_paths: Iterable[str], *, overwrite: bool) -> None:
    for relative_path in relative_paths:
        path = repo / relative_path
        if path.exists() and not overwrite:
            raise FileExistsError(f"{relative_path} already exists. Re-run with --overwrite to replace it.")


def _copy_example_glossary(target: Path) -> None:
    source = _project_root() / "glossary.example.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _indent_block(value: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else prefix for line in value.splitlines())


def _render_action_inputs(
    *,
    config_path: str,
    plugin_modules: Sequence[str],
    plugin_install_command: str | None,
) -> str:
    lines = [
        f"          config-file: {config_path}",
        "          openai-api-key: ${{ secrets.OPENAI_API_KEY }}",
        "          dry-run: true",
    ]
    if plugin_modules:
        lines.append(f"          plugin-modules: {','.join(plugin_modules)}")
    if plugin_install_command:
        lines.append("          plugin-install-command: |")
        lines.append(_indent_block(plugin_install_command, 12))
    return "\n".join(lines)


def _render_workflow(
    *,
    action_ref: str,
    config_path: str,
    base_branch: str,
    plugin_modules: Sequence[str],
    plugin_install_command: str | None,
) -> str:
    action_inputs = _render_action_inputs(
        config_path=config_path,
        plugin_modules=plugin_modules,
        plugin_install_command=plugin_install_command,
    )
    return f"""name: Translate
on:
  push:
    branches: [{base_branch}]
  workflow_dispatch: {{}}

permissions:
  contents: write
  pull-requests: write

jobs:
  translate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: bisq-network/localize-pipeline@{action_ref}
        with:
{action_inputs}
"""


def _render_onboarding_guide(
    *,
    config_path: str,
    glossary_path: str,
    workflow_path: str,
    action_ref: str,
    plugin_modules: Sequence[str],
    plugin_install_command: str | None,
) -> str:
    plugin_section = ""
    if plugin_modules or plugin_install_command:
        plugin_lines = ["## Custom Format Plugins", ""]
        if plugin_modules:
            plugin_lines.extend([
                "The generated workflow loads these adapter modules:",
                "",
                "```text",
                ",".join(plugin_modules),
                "```",
                "",
            ])
        if plugin_install_command:
            plugin_lines.extend([
                "It installs plugin dependencies with:",
                "",
                "```bash",
                plugin_install_command,
                "```",
                "",
            ])
        plugin_section = "\n".join(plugin_lines)

    return f"""# Localize Pipeline Onboarding

This repository was bootstrapped for Localize Pipeline. The generated workflow is
safe by default: it runs with `dry-run: true` until the first setup PR is merged
and the team explicitly enables translation writes.

## Files

- `{config_path}`: pipeline config generated from the detected localization files.
- `{glossary_path}`: starter glossary for project-specific terms.
- `{workflow_path}`: GitHub Action workflow pinned to `{action_ref}`.

## Validate Locally

```bash
python3 -m venv venv
./venv/bin/pip install localize-pipeline
./venv/bin/localize check --config {config_path}
./venv/bin/localize run --dry-run --config {config_path}
```

## Enable The GitHub Action

1. Add the `OPENAI_API_KEY` repository secret, or configure `api-base-url` for a
   local OpenAI-compatible endpoint.
2. Merge this onboarding PR while the workflow still has `dry-run: true`.
3. Create and review any initial locale backfill locally with
   `./venv/bin/localize run --config {config_path}`.
4. Set `dry-run: false` after the local baseline is merged.
5. Leave `process-all-files` unset for normal incremental translation runs.

Use `process-all-files: true` only for a controlled manual full scan or pilot
backfill, then return to the default incremental mode.

{plugin_section}## Maintenance

- Keep glossary changes reviewed like code.
- Use `localize memory stats --memory-file logs/translation_memory.json` to
  inspect the exact-match translation memory.
- Use `localize memory export` and `localize memory import` when sharing approved
  translation memory between projects.
"""


def _build_onboarding_config(options: BootstrapPrOptions, repo: Path, input_folder: str | None) -> str:
    if input_folder:
        input_folder_abs = repo / input_folder
        locales = detect_locales(
            str(input_folder_abs),
            source_locale=options.source_locale,
            localization_format=options.localization_format,
            localization_layout=options.localization_layout,
        )
        config = build_config(
            target_project_root=".",
            input_folder=input_folder,
            locales=locales,
            localization_format=options.localization_format,
            localization_layout=options.localization_layout,
            source_locale=options.source_locale,
            dry_run=True,
        )
        return render_config(config)

    detected = detect_project_layout(str(repo), source_locale=options.source_locale)
    if detected is None:
        raise RuntimeError(
            "Could not detect localization files. Pass --input-folder and, if needed, "
            "--localization-format/--localization-layout."
        )

    config = build_config(
        target_project_root=".",
        input_folder=detected.input_folder,
        locales=detected.locales,
        localization_profiles=[
            (profile_format, profile_layout)
            for profile_format, profile_layout in detected.localization_profiles
        ],
        source_locale=options.source_locale,
        dry_run=True,
    )
    if len(detected.localization_profiles) == 1:
        profile_format, profile_layout = detected.localization_profiles[0]
        config = build_config(
            target_project_root=".",
            input_folder=detected.input_folder,
            locales=detected.locales,
            localization_format=profile_format,
            localization_layout=profile_layout,
            source_locale=options.source_locale,
            dry_run=True,
        )
    return render_config(config)


def create_bootstrap_pr(options: BootstrapPrOptions) -> BootstrapPrResult:
    """Create a local onboarding branch and optionally push/open a PR."""
    repo = Path(options.target_project_root).expanduser().resolve()
    _assert_git_repo(repo)
    _assert_clean_worktree(repo)
    config_path = _repo_relative_path(repo, options.config_path, "config-file")
    glossary_path = _repo_relative_path(repo, options.glossary_path, "glossary-file")
    workflow_path = _repo_relative_path(repo, options.workflow_path, "workflow-file")
    onboarding_guide_path = (
        _repo_relative_path(repo, options.onboarding_guide_path, "onboarding-guide-file")
        if options.onboarding_guide_path
        else None
    )
    input_folder = (
        _repo_relative_path(repo, options.input_folder, "input-folder")
        if options.input_folder
        else None
    )
    created_files = tuple(
        path
        for path in (config_path, glossary_path, workflow_path, onboarding_guide_path)
        if path is not None
    )
    _assert_can_write(repo, created_files, overwrite=options.overwrite)
    base_branch = options.base_branch or "main"

    _checkout_onboarding_branch(repo, options, base_branch)

    (repo / config_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / config_path).write_text(_build_onboarding_config(options, repo, input_folder), encoding="utf-8")
    _copy_example_glossary(repo / glossary_path)
    workflow_file = repo / workflow_path
    workflow_file.parent.mkdir(parents=True, exist_ok=True)
    workflow_file.write_text(
        _render_workflow(
            action_ref=options.action_ref,
            config_path=config_path,
            base_branch=base_branch,
            plugin_modules=options.plugin_modules,
            plugin_install_command=options.plugin_install_command,
        ),
        encoding="utf-8",
    )
    if onboarding_guide_path:
        guide_file = repo / onboarding_guide_path
        guide_file.parent.mkdir(parents=True, exist_ok=True)
        guide_file.write_text(
            _render_onboarding_guide(
                config_path=config_path,
                glossary_path=glossary_path,
                workflow_path=workflow_path,
                action_ref=options.action_ref,
                plugin_modules=options.plugin_modules,
                plugin_install_command=options.plugin_install_command,
            ),
            encoding="utf-8",
        )

    _git(repo, "add", *created_files)
    _git(repo, "commit", "-m", options.commit_message)
    commit_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    pushed = False
    opened_pr = False
    if options.push or options.open_pr:
        _git(repo, "push", "-u", "origin", options.branch_name)
        pushed = True
    if options.open_pr:
        _run(
            (
                "gh",
                "pr",
                "create",
                "--title",
                options.pr_title,
                "--base",
                base_branch,
                "--body",
                "Adds Localize Pipeline config, glossary, and workflow in dry-run mode.",
            ),
            cwd=repo,
        )
        opened_pr = True

    return BootstrapPrResult(
        branch_name=options.branch_name,
        created_files=created_files,
        commit_sha=commit_sha,
        pushed=pushed,
        opened_pr=opened_pr,
    )
