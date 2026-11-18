"""Configured policies must cover unchanged targets after committed source edits."""

import json
import os
from pathlib import Path
from shlex import quote
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CASES = [
    ("java_properties", "suffix", "messages.properties", "messages_de.properties"),
    ("java_properties", "locale_directory", "en/messages.properties", "de/messages.properties"),
    ("json", "locale_directory", "en/messages.json", "de/messages.json"),
    ("json", "locale_filename", "en.json", "de.json"),
]


def _run(argv, cwd, *, env=None, check=True):
    return subprocess.run(argv, cwd=cwd, env=env, check=check, text=True, capture_output=True)


def _write(path, entries, format_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(entries, ensure_ascii=False, indent=2) if format_id == "json" else "\n".join(
        f"{key}={value}" for key, value in entries.items()
    )
    path.write_text(text + "\n", encoding="utf-8")


def _case(tmp_path, case, *, commit_source=True):
    format_id, layout, source_rel, target_rel = case
    repo = tmp_path / "target space"
    folder = repo / "l10n"
    folder.mkdir(parents=True)
    source, target = folder / source_rel, folder / target_rel
    old = {"remove": "Anyone may remove a record", "ignored": "Anyone may remove a record",
           "stable": "Only administrators may remove a record"}
    _write(source, old, format_id)
    _write(target, {key: "Jeder darf einen Eintrag entfernen" for key in old}, format_id)
    _run(["git", "init", "-q"], repo)
    for key, value in [("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgSign", "false")]:
        _run(["git", "config", key, value], repo)
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Original source and targets"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    _write(source, {**old, "remove": old["stable"], "ignored": old["stable"]}, format_id)
    _run(["git", "add", "."], repo)
    if commit_source:
        _run(["git", "commit", "-qm", "Restrict source action upstream"], repo)
    config = tmp_path / "config.yaml"
    key_prefix = "/" if format_id == "json" else ""
    config.write_text(yaml.safe_dump({
        "localization_formats": [{"id": format_id, "layout": {"id": layout}}] * 2,
        "supported_locales": [{"code": "de", "name": "German"}],
        "ignore_key_patterns": [f"^{key_prefix}ignored$"],
        "semantic_quality_rules": [{"id": "admin-only-action", "message": "Preserve the actor restriction",
            "source_regex": "Only administrators", "required_target_regex": "Administratoren"}],
    }), encoding="utf-8")
    return repo, folder, source, target, config, base


def _gate(repo, folder, config, output, *, base=None, changed_files=(), env=None):
    argv = [sys.executable, "-m", "localize.cli", "quality-gate", "--repo-root", str(repo),
            "--input-folder", str(folder), "--config", str(config),
            "--validation-summary", str(output.parent / "missing-summary.json"),
            "--output-json", str(output), "--output-markdown", str(output.with_suffix(".md"))]
    argv.extend(["--source-diff-base", base or "HEAD"])
    if changed_files:
        argv.extend(["--changed-files", *changed_files])
    return _run(argv, ROOT, env=env, check=False)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("commit_source", [False, True])
def test_source_changed_keys_apply_configured_rules_to_unchanged_targets(tmp_path, case, commit_source):
    repo, folder, _, target, config, base = _case(tmp_path, case, commit_source=commit_source)
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base=base if commit_source else None)
    assert output.exists(), result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert result.returncode == 1
    assert report["semantic_qa"]["errors_count"] == 1
    assert report["semantic_qa"]["examples"][0]["file"] == target.relative_to(folder).as_posix()
    assert report["semantic_qa"]["examples"][0]["rule_id"] == "admin-only-action"
    assert report["source_identical"]["changed_entries_count"] == 0


def test_changed_target_and_source_are_audited_once(tmp_path):
    repo, folder, _, target, config, base = _case(tmp_path, CASES[0])
    _write(target, {"remove": "Alle dürfen einen Eintrag entfernen"}, "java_properties")
    _run(["git", "add", "."], repo)
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base=base,
                   changed_files=[target.relative_to(repo).as_posix()])
    assert output.exists(), result.stderr
    assert json.loads(output.read_text())["semantic_qa"]["errors_count"] == 1


def test_unconfigured_source_change_does_not_trigger_a_new_heuristic_audit(tmp_path):
    repo, folder, _, _, config, base = _case(tmp_path, CASES[0])
    config.write_text("supported_locales: [{code: de, name: German}]\n")
    output = tmp_path / "report.json"
    # Deterministic Properties validation uses Git context without adding heuristics.
    result = _gate(repo, folder, config, output, base=base)
    assert output.exists(), result.stderr
    assert result.returncode == 0
    assert json.loads(output.read_text())["semantic_qa"]["findings_count"] == 0


def test_properties_source_audit_requires_valid_context_without_semantic_rules(tmp_path):
    repo, folder, _, _, config, _ = _case(tmp_path, CASES[0])
    config.write_text("supported_locales: [{code: de, name: German}]\n")
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base="missing-ref")
    assert result.returncode != 0
    assert "Git context is unavailable" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("blocking", [False, True])
def test_no_change_shell_path_retains_baseline_after_source_policy_failure(tmp_path, blocking):
    repo, folder, _, _, config, base = _case(tmp_path, CASES[0])
    if not blocking:
        raw = yaml.safe_load(config.read_text())
        raw["semantic_quality_rules"][0]["required_target_regex"] = "Jeder"
        config.write_text(yaml.safe_dump(raw))
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "localize").symlink_to(ROOT / "localize", target_is_directory=True)
    (logs / "translation_validation_summary.json").write_text('{"skipped_files": []}')
    script = (ROOT / "update-translations.sh").read_text()
    baseline = script[script.index("sanitize_state_component() {"):script.index("translation_file_change_regex() {")]
    collector = script[script.index("collect_changed_translation_files() {"):script.index("# Send a heartbeat")]
    publish = script[script.index("publish_translation_changes() {"):script.index("# Go back to original branch")]
    harness = f"""
set -eo pipefail
mapfile() {{
  shift
  local array_name="$1" line
  eval "$array_name=()"
  while IFS= read -r line; do eval "$array_name+=(\\\"\\$line\\\")"; done
}}
log() {{ :; }}
record_pipeline_event() {{ :; }}
translation_file_extension_regex() {{ printf properties; }}
{baseline}
{collector}
APP_ROOT={quote(str(tmp_path))}
TARGET_PROJECT_ROOT={quote(str(repo))}
ABSOLUTE_INPUT_FOLDER={quote(str(folder))}
CONFIG_FILE={quote(str(config))}
INPUT_FOLDER=l10n
LOCALIZE_STATE_DIR={quote(str(tmp_path / 'state'))}
LOG_DIR={quote(str(logs))}
UPSTREAM_REPO_NAME=example/project
DEFAULT_BRANCH=main
TRANSLATION_SOURCE=git
mkdir -p "$LOCALIZE_STATE_DIR"
printf '%s\\n' {quote(base)} > "$(git_source_baseline_path "$LOCALIZE_STATE_DIR")"
prepare_git_source_diff_base git
{publish}
"""
    env = dict(os.environ, PATH=f"{Path(sys.executable).parent}:{os.environ['PATH']}")
    env.pop("TRANSLATION_DIFF_BASE", None)
    result = _run(["bash", "-c", harness], repo, env=env, check=False)
    assert result.returncode == (1 if blocking else 0), result.stdout + result.stderr
    expected_base = base if blocking else _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert next((tmp_path / "state").glob("*.sha")).read_text().strip() == expected_base
    report = logs / "translation_quality_report-source-changes.json"
    assert report.exists(), result.stderr
    assert json.loads(report.read_text())["semantic_qa"]["errors_count"] == int(blocking)


@pytest.mark.parametrize("zero_base", [False, True])
def test_action_hands_committed_source_baseline_to_no_change_gate(tmp_path, zero_base):
    repo, _, _, _, config, base = _case(tmp_path, CASES[0])
    raw = yaml.safe_load(config.read_text())
    raw.update(target_project_root=str(repo), input_folder="l10n")
    config.write_text(yaml.safe_dump(raw))
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    step = next(s for s in action["runs"]["steps"] if s["name"] == "Run translation quality gate")
    env = dict(os.environ, PATH=f"{Path(sys.executable).parent}:{os.environ['PATH']}",
               PYTHONPATH=str(ROOT), TRANSLATOR_CONFIG_FILE=str(config),
               ACTION_QUALITY_REPORT_DIR=str(tmp_path / "reports"),
               LOCALIZE_ACTION_WORKSPACE=str(repo), DIFF_BASE_INPUT="0" * 40 if zero_base else base)
    result = _run(["bash", "-c", step["run"]], repo, env=env, check=False)
    report = tmp_path / "reports" / "translation_quality_report.json"
    assert report.exists(), result.stdout + result.stderr
    assert result.returncode == (0 if zero_base else 1)
    assert json.loads(report.read_text())["semantic_qa"]["errors_count"] == (0 if zero_base else 1)
    if zero_base:
        assert "::warning::" in result.stdout
        assert "committed source changes cannot be audited" in result.stdout
        assert "only uncommitted source changes are covered" in result.stdout
    assert step["env"]["DIFF_BASE_INPUT"] == "${{ inputs.diff-base }}"


def test_source_continuation_change_is_compared_as_a_complete_value(tmp_path):
    repo, folder, source, _, config, _ = _case(tmp_path, CASES[0])
    source.write_text("remove=Only \\\n  users may remove a record\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Source continuation"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    source.write_text("remove=Only \\\n  administrators may remove a record\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Change continuation only"], repo)
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base=base)
    assert output.exists(), result.stderr
    assert json.loads(output.read_text())["semantic_qa"]["errors_count"] == 1


def test_nested_json_source_change_keeps_full_pointer_identity(tmp_path):
    repo, folder, source, target, config, _ = _case(tmp_path, CASES[2])
    old = {"actions": {"remove": "Anyone may remove a record"},
           "other": {"remove": "Only administrators may remove a record"}}
    _write(source, old, "json")
    _write(target, {"actions": {"remove": "Jeder darf entfernen"},
                    "other": {"remove": "Jeder darf entfernen"}}, "json")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Nested source"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    _write(source, {**old, "actions": {"remove": old["other"]["remove"]}}, "json")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Change one nested source value"], repo)
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base=base)
    assert output.exists(), result.stderr
    stats = json.loads(output.read_text())["semantic_qa"]
    assert stats["errors_count"] == 1
    assert stats["examples"][0]["key"] == "/actions/remove"


def test_unreachable_source_baseline_fails_without_running_shell_text(tmp_path):
    repo, folder, _, _, config, _ = _case(tmp_path, CASES[0])
    marker = tmp_path / "executed"
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base=f"HEAD; touch {marker}")
    assert result.returncode != 0
    assert not output.exists()
    assert not marker.exists()
    assert "Source-change audit Git context is unavailable" in result.stderr


@pytest.mark.parametrize("case", [CASES[0], CASES[2]])
def test_utf8_baseline_preserves_unchanged_unicode_key_under_ascii_locale(tmp_path, case):
    repo, folder, source, target, config, _ = _case(tmp_path, case)
    old = {"remove": "Anyone may remove a record", "café": "Only administrators may remove a record"}
    _write(source, old, case[0])
    _write(target, {key: "Jeder darf einen Eintrag entfernen" for key in old}, case[0])
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Seed unchanged Unicode source key"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    _write(source, {**old, "remove": old["café"]}, case[0])
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-qm", "Change only the ASCII source key"], repo)
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    encoding = _run([sys.executable, "-c", "import locale; print(locale.getencoding())"], ROOT, env=env).stdout
    assert "utf" not in encoding.lower(), encoding
    output = tmp_path / "report.json"
    result = _gate(repo, folder, config, output, base=base, env=env)
    assert output.exists(), result.stderr
    assert result.returncode == 1
    stats = json.loads(output.read_text(encoding="utf-8"))["semantic_qa"]
    assert stats["errors_count"] == 1
    assert stats["examples"][0]["key"] == ("/remove" if case[0] == "json" else "remove")
