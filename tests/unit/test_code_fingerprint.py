"""Which code changes invalidate accepted translation checkpoints."""

import ast
from pathlib import Path
import shutil

import pytest

from localize import run_safeguards

PACKAGE = Path(run_safeguards.__file__).parent


@pytest.fixture
def package(tmp_path, monkeypatch):
    """Fingerprint a scratch copy of the package instead of the installed one."""
    root = tmp_path / "localize"
    shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(run_safeguards, "_PACKAGE_ROOT", root)
    yield root
    # The fingerprint is cached per process; drop the scratch value.
    run_safeguards.code_fingerprint.cache_clear()


def fingerprint():
    run_safeguards.code_fingerprint.cache_clear()
    return run_safeguards.code_fingerprint()


def append(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


@pytest.mark.parametrize("relative", [
    "guardian/state.py",
    "guardian/controller.py",
    "guardian/new_module.py",
])
def test_guardian_changes_keep_the_fingerprint(package, relative):
    before = fingerprint()
    append(package / relative, "\n# Guardian-only change\n")
    assert fingerprint() == before


@pytest.mark.parametrize("relative", [
    "translation_prompts.py",
    "formats/properties.py",
    "formats/validation.py",
    "translate_localization_files.py",
    "core/pipeline.py",
    # New translation modules count without being registered anywhere.
    "formats/new_format.py",
])
def test_translation_changes_change_the_fingerprint(package, relative):
    before = fingerprint()
    append(package / relative, "\n# Translation change\n")
    assert fingerprint() != before


def guardian_imports(path):
    """Yield every module name under ``localize.guardian`` that *path* imports."""
    package = ".".join(("localize",) + path.relative_to(PACKAGE).parent.parts)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = node.module or ""
            if node.level:
                base = ".".join(parts[:len(parts) - node.level + 1] + ([base] if base else []))
            names = [base] + [f"{base}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]  # importlib.import_module("localize.guardian...")
        else:
            continue
        yield from (
            name for name in names
            if name == "localize.guardian" or name.startswith("localize.guardian.")
        )


def test_only_the_guardian_command_imports_the_guardian():
    """The fingerprint may skip the Guardian only while translation never loads it."""
    found = {
        (str(path.relative_to(PACKAGE)), name)
        for path in PACKAGE.rglob("*.py")
        if path.relative_to(PACKAGE).parts[0] != "guardian"
        for name in guardian_imports(path)
    }
    assert found <= {("cli.py", "localize.guardian.cli")}
