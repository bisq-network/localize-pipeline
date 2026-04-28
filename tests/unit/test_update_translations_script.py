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

    match = re.search(r"^MAX_FILES_PER_PR=\$\{MAX_FILES_PER_PR:-(\d+)\}", script, re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) == 150


def test_env_example_documents_max_files_per_pr_override():
    env_example = (REPO_ROOT / "docker" / ".env.example").read_text()

    assert "MAX_FILES_PER_PR=150" in env_example
