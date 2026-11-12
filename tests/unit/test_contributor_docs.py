"""Keep the contributor guide and the repository map aligned with the code."""

from __future__ import annotations

import re
from pathlib import Path

from localize.cli import _build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRIBUTING = (PROJECT_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
STRUCTURE = (PROJECT_ROOT / "docs" / "repository-structure.md").read_text(
    encoding="utf-8"
)


def _lockfile_command(lockfile_name: str) -> str:
    """Return the pip-compile command recorded in a lockfile header."""
    header = (PROJECT_ROOT / lockfile_name).read_text(encoding="utf-8")[:600]
    match = re.search(r"^#\s+(pip-compile .+)$", header, flags=re.MULTILINE)
    assert match is not None, f"no pip-compile command in {lockfile_name}"
    return match.group(1).strip()


def test_contributing_setup_installs_the_tools_it_tells_people_to_run():
    """A newcomer who follows the guide must end up with pytest and ruff."""
    dev_inputs = (PROJECT_ROOT / "requirements-dev.in").read_text(encoding="utf-8")

    assert "pip install -r requirements-dev.txt" in CONTRIBUTING
    for tool in ("pytest", "ruff", "pip-tools"):
        assert re.search(rf"^{tool}\b", dev_inputs, flags=re.MULTILINE), tool
    assert "ruff check ." in CONTRIBUTING
    assert "pytest" in CONTRIBUTING
    # Tools that are not development dependencies must not be recommended.
    assert "black" not in CONTRIBUTING.lower()
    assert "bisq-network/localize-pipeline" in CONTRIBUTING


def test_contributing_lockfile_commands_match_the_lockfile_headers():
    """Regenerating a lockfile with the documented command must not drop pins."""
    for lockfile_name in ("requirements.txt", "requirements-dev.txt"):
        assert _lockfile_command(lockfile_name) in CONTRIBUTING


def test_repository_structure_lists_every_package_module():
    """Every module outside the Guardian package has a row in the map."""
    package_root = PROJECT_ROOT / "localize"
    for module_path in sorted(package_root.rglob("*.py")):
        relative = module_path.relative_to(PROJECT_ROOT).as_posix()
        if module_path.name == "__init__.py" or relative.startswith(
            "localize/guardian/"
        ):
            continue
        assert f"`{relative}`" in STRUCTURE, f"{relative} is not documented"
    for package in ("core", "formats", "providers", "guardian"):
        assert f"`localize/{package}/`" in STRUCTURE or f"`{package}`" in STRUCTURE


def test_repository_structure_lists_every_cli_command():
    """The CLI row names each top-level `localize` subcommand."""
    parser = _build_parser()
    commands = next(
        action.choices
        for action in parser._actions
        if getattr(action, "choices", None) and "run" in action.choices
    )
    cli_row = next(
        line for line in STRUCTURE.splitlines() if line.startswith("| `localize/cli.py`")
    )
    for command in commands:
        assert re.search(rf"[ /]{re.escape(command)}[/`]", cli_row), command


def test_repository_structure_lists_every_guide_and_no_missing_path():
    """Each guide in docs/ is listed, and each listed path exists."""
    for guide in sorted((PROJECT_ROOT / "docs").glob("*.md")):
        assert f"`docs/{guide.name}`" in STRUCTURE, guide.name

    listed_paths = re.findall(r"^\| `([^`]+)`", STRUCTURE, flags=re.MULTILINE)
    assert listed_paths
    for listed in listed_paths:
        if listed == "secrets/":
            continue  # Git-ignored on purpose; created by the operator.
        if "." not in listed.split("/")[-1] and not listed.endswith("/"):
            continue  # Old-to-new module names in the migration table.
        assert (PROJECT_ROOT / listed).exists(), f"{listed} does not exist"
