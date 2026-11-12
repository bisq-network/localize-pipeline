"""Keep the contributor guide aligned with the code."""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRIBUTING = (PROJECT_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


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
