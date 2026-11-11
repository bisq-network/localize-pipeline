#!/usr/bin/env python3
"""Prepare or run Bisq 1 translations locally, leaving a reviewed patch candidate."""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from localize.formats.properties import parse_properties_file  # noqa: E402

UPSTREAM = "https://github.com/bisq-network/bisq.git"
RESOURCE_PATH = "core/src/main/resources/i18n"
BRANCH = "codex/bisq1-translations"
# Last full translation import before the recent source/security changes.
# Used only to bootstrap a new ledger, never to replace an existing ledger.
INITIAL_BASE = "8e19e4b74f73a32e3426ca55c674e46c4cb4f9af"


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()


def prepare_checkout(repo):
    if not repo.exists():
        subprocess.run([
            "git", "clone", "--filter=blob:none", "--sparse", "--single-branch",
            "--branch", "master", UPSTREAM, str(repo),
        ], check=True)
        git(repo, "sparse-checkout", "set", RESOURCE_PATH)
        git(repo, "checkout", "-b", BRANCH)
        with (repo / ".git/info/exclude").open("a") as stream:
            stream.write(f"\n/{RESOURCE_PATH}/archive/\n")
    if git(repo, "remote", "get-url", "origin") != UPSTREAM:
        raise RuntimeError("Workspace origin is not the expected Bisq 1 repository.")
    if git(repo, "branch", "--show-current") != BRANCH:
        raise RuntimeError(f"Workspace must be on {BRANCH}; preserve/review other branches first.")
    if git(repo, "status", "--porcelain"):
        raise RuntimeError("Workspace has pending changes. Review them before another run.")
    git(repo, "fetch", "--no-tags", "origin", "master")
    if subprocess.run([
        "git", "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", "origin/master",
    ], check=False).returncode:
        raise RuntimeError("Local commits are not yet in upstream master; review/merge them first.")
    git(repo, "merge", "--ff-only", "origin/master")


def write_config(repo, state, locales, report_dir=None):
    report_dir = report_dir or state
    config = yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())
    known = {row["code"] for row in config["supported_locales"]}
    if set(locales) - known:
        raise ValueError(f"Unknown Bisq 1 locale(s): {sorted(set(locales) - known)}")
    if locales:
        config["supported_locales"] = [
            row for row in config["supported_locales"] if row["code"] in locales
        ]
    config.update({
        "target_project_root": str(repo),
        "glossary_file_path": str(ROOT / "profiles/bisq1/glossary.json"),
        "translation_queue_folder": str(report_dir / "queue"),
        "translated_queue_folder": str(report_dir / "translated"),
        "translation_key_ledger_file_path": str(report_dir / "ledger.json"),
        "translation_memory_file_path": str(report_dir / "memory.json"),
        "logging": {"log_level": "INFO", "log_to_console": True,
                    "log_file_path": str(report_dir / "logs/translation.log")},
    })
    config["run_safeguards"]["state_path"] = str(state / "run_safeguards.sqlite3")
    path = report_dir / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    return path


def baseline_ledger(runtime, repo, state, base, filenames):
    ledger_path = state / "ledger.json"
    ledger = runtime.load_translation_key_ledger(str(ledger_path)) if ledger_path.exists() else {}
    missing = [filename for filename in filenames if filename not in ledger]
    if not missing:
        return ledger
    base = git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    subprocess.run([
        "git", "-C", str(repo), "merge-base", "--is-ancestor", base, "HEAD",
    ], check=True)
    baseline_path = state / "baseline-source.properties"
    baseline_path.write_text(git(repo, "show", f"{base}:{RESOURCE_PATH}/displayStrings.properties"))
    old_source = parse_properties_file(str(baseline_path))[1]
    for filename in missing:
        # Historical source hashes select changed English even when a target
        # already contains a non-English value. Missing/blank keys are selected
        # by the existing pipeline independently.
        targets = parse_properties_file(str(repo / RESOURCE_PATH / filename))[1]
        ledger[filename] = runtime.build_file_key_ledger(old_source, targets)
    return ledger


def require_complete_run(result):
    if result is False:
        raise RuntimeError("Translation pipeline failed.")
    if result.skipped_files:
        raise RuntimeError(f"Translation files were skipped: {sorted(result.skipped_files)}")


def seed_run_memory(state, report_dir, resume_path=None):
    """Reuse explicitly reviewed memory without advancing accepted job state."""
    from localize.translation_memory import load_translation_memory_strict, write_translation_memory

    memory = load_translation_memory_strict(state / "memory.json")
    if resume_path is not None:
        memory.merge_from(load_translation_memory_strict(resume_path, require_exists=True))
    if memory.entries:
        write_translation_memory(report_dir / "memory.json", memory)


def reconcile_run_state(runtime, repo, report_dir, filenames):
    """Match successful report state to final files, without approving new memory."""
    from localize.translation_memory import load_translation_memory_strict, memory_source_hash, write_translation_memory

    ledger_path = report_dir / "ledger.json"
    memory_path = report_dir / "memory.json"
    ledger = runtime.load_translation_key_ledger(str(ledger_path))
    memory = load_translation_memory_strict(memory_path)
    sources = parse_properties_file(str(repo / RESOURCE_PATH / "displayStrings.properties"))[1]
    invalidated = set()
    for filename in filenames:
        locale = runtime.extract_language_from_filename(filename, list(runtime.LANGUAGE_CODES))
        if locale is None:
            raise ValueError(f"Unknown final target locale: {filename}")
        targets = parse_properties_file(str(repo / RESOURCE_PATH / filename))[1]
        entries = ledger.get(filename, {})
        for key, value in targets.items():
            if key not in entries:
                raise RuntimeError(f"Final key has no validated ledger entry: {filename}:{key}")
            entry = entries[key]
            target_hash = runtime.compute_ledger_hash(value)
            if entry.get("target_hash") == target_hash:
                continue
            source = sources.get(key, "")
            if entry.get("source_hash") != runtime.compute_ledger_hash(source):
                raise RuntimeError(f"Source changed after validation: {filename}:{key}")
            entry["target_hash"] = target_hash
            identity = f"java_properties:{locale}:{memory_source_hash(source)}"
            cached = memory.entries.get(identity)
            if cached and cached.get("status") != "conflict" and cached.get("target") != value:
                invalidated.add(identity)
        ledger[filename] = entries
    for identity in invalidated:
        memory.entries.pop(identity, None)
    runtime.save_translation_key_ledger(str(ledger_path), ledger)
    if memory_path.exists():
        write_translation_memory(memory_path, memory)
    return sorted(invalidated)


def run(args, state):
    repo = state / "bisq"
    prepare_checkout(repo)
    report_dir = state / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_dir.mkdir(parents=True)
    config_path = write_config(repo, state, args.locales, report_dir)
    (state / "latest-run.txt").write_text(str(report_dir) + "\n")
    load_dotenv(state / ".env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    os.environ["TRANSLATOR_CONFIG_FILE"] = str(config_path)
    os.environ["LOCALIZE_DRY_RUN"] = "true" if args.check else "false"
    if not args.check and not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError(f"Set OPENAI_API_KEY in your shell or {state / '.env'}.")

    from localize import translate_localization_files as runtime
    from localize.translation_quality_gate import main as quality_gate

    # The existing pipeline writes its reports relative to PROJECT_ROOT_DIR.
    # Keep this job's reports separate from every other profile's reports.
    runtime.PROJECT_ROOT_DIR = str(report_dir)
    filenames = [
        f"displayStrings_{code}.properties" for code in runtime.LANGUAGE_CODES
    ]
    ledger = baseline_ledger(runtime, repo, state, args.base_ref, filenames)
    source_path = repo / RESOURCE_PATH / "displayStrings.properties"
    source_bytes = source_path.read_bytes()
    source = parse_properties_file(str(source_path))[1]
    plan = {}
    for filename in filenames:
        lines, targets = parse_properties_file(str(repo / RESOURCE_PATH / filename))
        _, _, keys = runtime.extract_texts_to_translate(
            lines, source, targets, file_ledger_entries=ledger.get(filename, {}),
        )
        plan[filename] = {"keys_to_translate": len(keys)}
    receipt = {
        "upstream_head": git(repo, "rev-parse", "HEAD"),
        "bootstrap_source_ref": args.base_ref,
        "existing_ledger": (state / "ledger.json").exists(),
        "files": plan, "model_calls": "none (preflight)",
    }
    (report_dir / "plan.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2), flush=True)
    if args.check:
        print(f"Preflight complete; no translations generated. Report: {report_dir / 'plan.json'}")
        return

    # Keep accepted persistent state unchanged until the entire run passes.
    runtime.save_translation_key_ledger(str(report_dir / "ledger.json"), ledger)
    seed_run_memory(state, report_dir, args.resume_memory)
    # General discovery deliberately includes unknown suffix locales so other
    # jobs can report them. This manual job owns only the selected exact files.
    allowed_filenames = set(filenames)
    discover = runtime.get_changed_translation_files

    def discover_selected(*args, **kwargs):
        return [filename for filename in discover(*args, **kwargs)
                if filename in allowed_filenames]

    runtime.get_changed_translation_files = discover_selected
    try:
        result = asyncio.run(runtime.main())
    finally:
        runtime.get_changed_translation_files = discover
    require_complete_run(result)
    if source_path.read_bytes() != source_bytes:
        raise RuntimeError("English source changed unexpectedly; inspect the workspace.")
    changed = git(repo, "diff", "--name-only").splitlines()
    allowed = {f"{RESOURCE_PATH}/{filename}" for filename in filenames}
    if set(changed) - allowed:
        raise RuntimeError("Unexpected files changed; inspect the workspace.")
    if changed:
        git(repo, "add", "--", *changed)
        from localize.translation_semantic_reviewer import main as semantic_review
        if semantic_review([
            "--repo-root", str(repo), "--input-folder", RESOURCE_PATH,
            "--config", str(config_path),
            "--validation-summary", str(report_dir / "logs/translation_validation_summary.json"),
            "--token-usage-summary", str(report_dir / "logs/token_usage_summary.json"),
            "--changed-files", *changed,
        ]):
            raise RuntimeError("Semantic review failed; local candidate retained for inspection.")
        # The sidecar may apply reviewed suggestions; include them in the gate.
        git(repo, "add", "--", *changed)
    status = quality_gate([
        "--repo-root", str(repo), "--input-folder", RESOURCE_PATH,
        "--config", str(config_path),
        "--validation-summary", str(report_dir / "logs/translation_validation_summary.json"),
        "--output-json", str(report_dir / "quality.json"),
        "--output-markdown", str(report_dir / "quality.md"),
        "--changed-files", *(changed or sorted(allowed)),
    ])
    patch = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
    (report_dir / "translations.patch").write_bytes(patch)
    if status:
        raise RuntimeError(f"Quality gate failed. Review {report_dir / 'quality.md'}; do not publish.")
    reconcile_run_state(runtime, repo, report_dir, filenames)
    for filename in ("ledger.json", "memory.json"):
        if (report_dir / filename).exists():
            temporary = state / f"{filename}.tmp"
            shutil.copyfile(report_dir / filename, temporary)
            temporary.replace(state / filename)
    print(f"Translation and quality checks passed. Review: {report_dir / 'quality.md'}")
    print(f"Local patch: {report_dir / 'translations.patch'}")
    print(f"Changes are staged in {repo}. No commit, push or Transifex upload was made.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fetch and inspect; no model calls.")
    parser.add_argument("--locales", nargs="+", default=[], help="Optional subset, e.g. de pt-br.")
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".agent-artifacts/bisq1-local")
    parser.add_argument("--base-ref", default=INITIAL_BASE,
                        help="Source baseline used only when bootstrapping a new ledger.")
    parser.add_argument("--resume-memory", type=Path,
                        help="Explicitly reviewed memory from a prior run; seeds only this run and repeats quality checks.")
    args = parser.parse_args()
    state = args.work_dir.expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "job.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run(args, state)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
