"""Exercise the shell publisher against imported, explicitly skipped files."""

import json
import os
from pathlib import Path
from shlex import quote
import subprocess
import sys

import pytest

from tests.integration.test_update_translations_batches import _run


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("dry_run,valid_file,new_file", [(False, True, False), (False, False, False), (True, True, False), (False, True, True)])
def test_skipped_import_never_reaches_publication(tmp_path, dry_run, valid_file, new_file):
    """Keep the imported English regression out of commits and preserve evidence."""
    repo = tmp_path / "target"
    repo.mkdir()
    folder = repo / "l10n"
    folder.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "localize").symlink_to(ROOT / "localize", target_is_directory=True)
    skipped = folder / "settings_pt_PT.properties"
    valid = folder / "settings_ta.properties"
    original = "label=Opções\nhelp=Escolha as opções\nunrelated=Valor\n"
    imported = "label=Options\nhelp=Choose options\nunrelated=Valor\x7f\n"
    skipped.write_text(original)
    valid.write_text("label=Original\n")
    _run(["git", "init", "-q"], cwd=repo)
    for key, value in [("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgSign", "false")]:
        _run(["git", "config", key, value], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-qm", "Original"], cwd=repo)
    if new_file:
        skipped = folder / "new_pt_PT.properties"
    skipped.write_text(imported)
    if valid_file:
        valid.write_text("label=மொழிபெயர்ப்பு\n")
    (logs / "translation_validation_summary.json").write_text(json.dumps({
        "files": {"settings_ta.properties": {}} if valid_file else {},
        "skipped_files": [skipped.name],
        "pipeline_warnings": [{"file": skipped.name, "errors": ["U+007F"]}],
    }))
    script = (ROOT / "update-translations.sh").read_text()
    collector = script[script.index("collect_changed_translation_files() {"):script.index("# Send a heartbeat")]
    publish = script[script.index("publish_translation_changes() {"):script.index("# Go back to original branch")]
    harness = rf"""
set -euo pipefail
mapfile() {{
  shift
  local array_name="$1" line
  eval "$array_name=()"
  while IFS= read -r line; do eval "$array_name+=(\"\$line\")"; done
}}
log() {{ printf '%s\n' "$1"; }}
record_pipeline_event() {{ :; }}
command_exists() {{ command -v "$1" >/dev/null; }}
translation_file_extension_regex() {{ printf properties; }}
update_git_source_baseline_if_safe() {{ touch {quote(str(tmp_path / 'baseline-advanced'))}; }}
stage_and_submit_batch() {{
  printf '%s\n' "${{BATCH_FILES[@]}}" > {quote(str(tmp_path / 'reviewed-files'))}
  git add -- "${{BATCH_FILES[@]}}"
  git -c commit.gpgSign=false commit -qm Translations
}}
{collector}
APP_ROOT={quote(str(tmp_path))}
TARGET_PROJECT_ROOT={quote(str(repo))}
ABSOLUTE_INPUT_FOLDER={quote(str(folder))}
INPUT_FOLDER=l10n
DRY_RUN={str(dry_run).lower()}
MAX_FILES_PER_PR=90
TRANSLATION_BRANCH_PREFIX=translation-updates
FORK_REPO_NAME=owner/repo
TRANSLATION_SOURCE=transifex
git remote add origin git@github.com:owner/repo.git
{publish}
touch {quote(str(tmp_path / 'heartbeat'))}
"""
    env = dict(os.environ, PATH=f"{Path(sys.executable).parent}:{os.environ['PATH']}")
    env.pop("PYTHONPATH", None)
    result = subprocess.run(["bash", "-c", harness], cwd=repo, env=env, text=True, capture_output=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (tmp_path / "baseline-advanced").exists()
    assert not (tmp_path / "heartbeat").exists()
    reviewed = tmp_path / "reviewed-files"
    if valid_file and not dry_run:
        assert reviewed.read_text().splitlines() == ["l10n/settings_ta.properties"]
        assert _run(["git", "show", "HEAD:l10n/settings_pt_PT.properties"], cwd=repo).stdout == original
    else:
        assert not reviewed.exists()
    evidence = list(logs.glob("skipped-inputs-*/" + skipped.name))
    assert len(evidence) == 1
    assert evidence[0].read_text() == imported
    assert skipped.read_text() == imported
