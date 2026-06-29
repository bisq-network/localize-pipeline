"""Command line interface for the reusable localization pipeline."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from localize.app_config import ConfigIssue, validate_config
from localize.bootstrap_pr import BootstrapPrOptions, create_bootstrap_pr
from localize.formats import list_localization_adapters, list_localization_formats
from localize.init_config import main as init_config_main
from localize.localization_profiles import load_localization_profiles
from localize.plugins import load_plugins
from localize.translation_quality_gate import main as translation_quality_gate_main
from localize.translation_memory import (
    load_translation_memory_strict,
    load_translation_memory,
    merge_translation_memory,
    translation_memory_suggestions,
    write_translation_memory,
)


def _load_config_file(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Configuration file must contain a YAML mapping.")
    return payload


def _effective_api_base_url(config: dict[str, Any]) -> str | None:
    for candidate in (os.environ.get("OPENAI_BASE_URL"), config.get("api_base_url")):
        if candidate is not None:
            stripped = str(candidate).strip()
            if stripped:
                return stripped
    return None


def _effective_dry_run_override() -> bool | None:
    raw_value = os.environ.get("LOCALIZE_DRY_RUN")
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    return None


def _print_config_issues(issues: Sequence[ConfigIssue]) -> None:
    for issue in issues:
        print(f"{issue.level}: {issue.message}", file=sys.stderr)


def _cmd_formats(_args: argparse.Namespace) -> int:
    adapters = list_localization_adapters()
    for format_id, localization_format in sorted(list_localization_formats().items()):
        adapter_status = "adapter=yes" if format_id in adapters else "adapter=no"
        print(
            f"{format_id}\t{localization_format.file_extension}\t"
            f"{localization_format.display_name}\t{adapter_status}"
        )
    return 0


def _cmd_validate_config(args: argparse.Namespace, *, success_message: str) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"error: configuration file not found: {config_path}", file=sys.stderr)
        return 1
    try:
        config = _load_config_file(config_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: could not read configuration: {exc}", file=sys.stderr)
        return 1

    review_model_override = os.environ.get("REVIEW_MODEL_NAME")
    if review_model_override:
        config = {**config, "review_model_name": review_model_override}
    issues = validate_config(
        config,
        effective_api_base_url=_effective_api_base_url(config),
        api_key_available=bool(os.environ.get("OPENAI_API_KEY")),
        dry_run_override=_effective_dry_run_override(),
    )
    _print_config_issues(issues)
    has_errors = any(issue.level == "error" for issue in issues)
    if has_errors:
        return 1
    print(success_message)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    return _cmd_validate_config(args, success_message="Configuration OK")


def _cmd_check(args: argparse.Namespace) -> int:
    return _cmd_validate_config(args, success_message="Preflight OK")


def _resolve_input_folder(config: dict[str, Any]) -> Path:
    target_root = Path(str(config.get("target_project_root") or ".")).expanduser()
    input_folder = Path(str(config.get("input_folder") or ".")).expanduser()
    if not input_folder.is_absolute():
        input_folder = target_root / input_folder
    return input_folder.resolve()


def _runtime_dir(raw_path: Any, *, config_path: Path) -> Path:
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def _load_checked_config(config_path: Path) -> tuple[dict[str, Any], list[ConfigIssue]]:
    config = _load_config_file(config_path)
    review_model_override = os.environ.get("REVIEW_MODEL_NAME")
    if review_model_override:
        config = {**config, "review_model_name": review_model_override}
    issues = validate_config(
        config,
        effective_api_base_url=_effective_api_base_url(config),
        api_key_available=bool(os.environ.get("OPENAI_API_KEY")),
        dry_run_override=_effective_dry_run_override(),
    )
    return config, issues


def _cmd_doctor(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"error: configuration file not found: {config_path}", file=sys.stderr)
        return 1
    try:
        config, issues = _load_checked_config(config_path)
        profiles = load_localization_profiles(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: could not inspect configuration: {exc}", file=sys.stderr)
        return 1

    profile = os.environ.get("TRANSLATOR_PROFILE") or "default"
    target_root = Path(str(config.get("target_project_root") or ".")).expanduser().resolve()
    input_folder = _resolve_input_folder(config)
    semantic_review = config.get("semantic_review", {}) or {}
    semantic_status = "enabled" if bool(semantic_review.get("enabled", False)) else "disabled"
    api_base_url = _effective_api_base_url(config) or "default"
    model_provider = str(config.get("model_provider", "aisuite"))
    queue = _runtime_dir(config.get("translation_queue_folder", "translation_queue"), config_path=config_path)
    translated = _runtime_dir(config.get("translated_queue_folder", "translated_queue"), config_path=config_path)
    formats = ", ".join(
        f"{profile.localization_format.id}/{profile.localization_layout.id}"
        for profile in profiles
    )

    print("Localize Pipeline doctor")
    print(f"profile: {profile}")
    print(f"config: {config_path}")
    print(f"target_project_root: {target_root}")
    print(f"input_folder: {input_folder}")
    print(f"translation_queue_folder: {queue}")
    print(f"translated_queue_folder: {translated}")
    print(f"localization_profiles: {formats}")
    print(f"model_provider: {model_provider}")
    print(f"model_name: {config.get('model_name', 'gpt-4')}")
    print(f"review_model_name: {config.get('review_model_name', config.get('model_name', 'gpt-4'))}")
    print(f"api_base_url: {api_base_url}")
    print(f"semantic_review: {semantic_status}")
    print(f"OPENAI_API_KEY: {'set' if os.environ.get('OPENAI_API_KEY') else 'unset'}")
    _print_config_issues(issues)
    return 1 if any(issue.level == "error" for issue in issues) else 0


def _cmd_smoke(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"error: configuration file not found: {config_path}", file=sys.stderr)
        return 1
    try:
        config, issues = _load_checked_config(config_path)
        load_localization_profiles(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: smoke check failed: {exc}", file=sys.stderr)
        return 1
    _print_config_issues(issues)
    if any(issue.level == "error" for issue in issues):
        return 1

    queue = _runtime_dir(config.get("translation_queue_folder", "translation_queue"), config_path=config_path)
    translated = _runtime_dir(config.get("translated_queue_folder", "translated_queue"), config_path=config_path)
    queue.mkdir(parents=True, exist_ok=True)
    translated.mkdir(parents=True, exist_ok=True)
    print("Smoke OK")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    os.environ["TRANSLATOR_CONFIG_FILE"] = str(config_path)
    if args.dry_run:
        os.environ["LOCALIZE_DRY_RUN"] = "true"
    runtime = importlib.import_module("localize.translate_localization_files")
    asyncio.run(runtime.main())
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    return init_config_main(list(args.init_args))


def _cmd_bootstrap_pr(args: argparse.Namespace) -> int:
    try:
        result = create_bootstrap_pr(
            BootstrapPrOptions(
                target_project_root=args.target_project_root,
                input_folder=args.input_folder,
                localization_format=args.localization_format,
                localization_layout=args.localization_layout,
                source_locale=args.source_locale,
                config_path=args.config_file,
                glossary_path=args.glossary_file,
                workflow_path=args.workflow_file,
                branch_name=args.branch,
                base_branch=args.base_branch,
                action_ref=args.action_ref,
                overwrite=args.overwrite,
                reset_branch=args.reset_branch,
                onboarding_guide_path=None if args.skip_onboarding_guide else args.onboarding_guide_file,
                plugin_modules=tuple(args.plugin_module),
                plugin_install_command=args.plugin_install_command,
                push=args.push,
                open_pr=args.open_pr,
            )
        )
    except (FileExistsError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created onboarding commit {result.commit_sha} on branch {result.branch_name}")
    if result.opened_pr:
        print("Opened onboarding pull request")
    elif result.pushed:
        print("Pushed onboarding branch")
    else:
        print("Review the branch, then push/open a pull request when ready")
    return 0


def _cmd_quality_gate(args: argparse.Namespace) -> int:
    quality_args = [
        "--repo-root",
        args.repo_root,
        "--input-folder",
        args.input_folder,
        "--config",
        args.config,
        "--validation-summary",
        args.validation_summary,
        "--output-json",
        args.output_json,
        "--output-markdown",
        args.output_markdown,
    ]
    if args.audit_scope:
        quality_args.extend(["--audit-scope", args.audit_scope])
    quality_args.append("--changed-files")
    quality_args.extend(args.changed_files)
    return int(translation_quality_gate_main(quality_args))


def _print_memory_stats(args: argparse.Namespace) -> int:
    memory = load_translation_memory(args.memory_file)
    stats = memory.stats()
    if args.output_format == "json":
        print(json.dumps({
            "total_entries": stats.total_entries,
            "active_entries": stats.active_entries,
            "conflict_entries": stats.conflict_entries,
            "locales": list(stats.locales),
            "formats": list(stats.formats),
        }, ensure_ascii=False, sort_keys=True))
        return 0

    print(f"total_entries: {stats.total_entries}")
    print(f"active_entries: {stats.active_entries}")
    print(f"conflict_entries: {stats.conflict_entries}")
    print(f"locales: {', '.join(stats.locales) if stats.locales else '-'}")
    print(f"formats: {', '.join(stats.formats) if stats.formats else '-'}")
    return 0


def _cmd_memory_export(args: argparse.Namespace) -> int:
    try:
        memory = load_translation_memory_strict(args.memory_file, require_exists=True)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        write_translation_memory(args.output, memory)
    except OSError as exc:
        print(f"error: could not export translation memory: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {memory.stats().total_entries} translation memory entries to {args.output}")
    return 0


def _cmd_memory_import(args: argparse.Namespace) -> int:
    try:
        target = load_translation_memory_strict(args.memory_file)
        incoming = load_translation_memory_strict(args.input, require_exists=True)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = merge_translation_memory(target, incoming)
    try:
        write_translation_memory(args.memory_file, target)
    except OSError as exc:
        print(f"error: could not import translation memory: {exc}", file=sys.stderr)
        return 1
    print(
        "Imported translation memory: "
        f"imported={result.imported_entries} "
        f"unchanged={result.unchanged_entries} "
        f"conflicts={result.conflict_entries}"
    )
    return 0


def _cmd_memory_promote(args: argparse.Namespace) -> int:
    try:
        memory = load_translation_memory_strict(args.memory_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    memory.record(
        args.source_text,
        args.target_text,
        locale=args.locale,
        format_id=args.format_id,
    )
    try:
        write_translation_memory(args.memory_file, memory)
    except OSError as exc:
        print(f"error: could not promote translation memory entry: {exc}", file=sys.stderr)
        return 1
    print("Promoted translation memory entry")
    return 0


def _cmd_memory_suggest(args: argparse.Namespace) -> int:
    try:
        memory = load_translation_memory_strict(args.memory_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    suggestions = translation_memory_suggestions(
        memory,
        args.source_text,
        locale=args.locale,
        format_id=args.format_id,
        min_score=args.min_score,
        limit=args.limit,
    )
    for suggestion in suggestions:
        print(
            f"{suggestion.score:.3f}\t{suggestion.locale}\t{suggestion.format_id}\t"
            f"{suggestion.source_text}\t{suggestion.target_text}"
        )
    return 0


def _non_negative_int(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return value


def _extract_plugin_args(raw_argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return plugin modules and argv with all --plugin flags removed."""
    plugins: list[str] = []
    command_argv: list[str] = []
    index = 0
    while index < len(raw_argv):
        argument = raw_argv[index]
        if argument == "--plugin":
            if index + 1 >= len(raw_argv):
                raise ValueError("argument --plugin: expected one argument")
            plugins.append(raw_argv[index + 1])
            index += 2
            continue
        if argument.startswith("--plugin="):
            plugin = argument.split("=", 1)[1]
            if not plugin:
                raise ValueError("argument --plugin: expected one argument")
            plugins.append(plugin)
            index += 1
            continue
        command_argv.append(argument)
        index += 1
    return plugins, command_argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localize",
        description="Validate and run the AI localization translation pipeline.",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help=(
            "Import a plugin module before running the command. Plugins register "
            "custom localization adapters at import time."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    formats_parser = subparsers.add_parser(
        "formats",
        help="List registered localization formats and runtime adapters.",
    )
    formats_parser.set_defaults(func=_cmd_formats)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a localization pipeline config without running translation.",
    )
    validate_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    validate_parser.set_defaults(func=_cmd_validate)

    check_parser = subparsers.add_parser(
        "check",
        help="Run self-service preflight checks before translation.",
    )
    check_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    check_parser.set_defaults(func=_cmd_check)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Print redacted effective config for deploy/debug checks.",
    )
    doctor_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    doctor_parser.set_defaults(func=_cmd_doctor)

    smoke_parser = subparsers.add_parser(
        "smoke",
        help="Run read-only startup checks and create runtime scratch directories.",
    )
    smoke_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    smoke_parser.set_defaults(func=_cmd_smoke)

    run_parser = subparsers.add_parser(
        "run",
        help="Run the configured translation pipeline.",
    )
    run_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode for this invocation without editing config.yaml.",
    )
    run_parser.set_defaults(func=_cmd_run)

    init_parser = subparsers.add_parser(
        "init",
        add_help=False,
        help="Scaffold a minimal config by detecting locales in an input folder.",
    )
    init_parser.set_defaults(func=_cmd_init, init_args=())

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-pr",
        help="Create an onboarding branch with config, glossary, and GitHub workflow.",
    )
    bootstrap_parser.add_argument("--target-project-root", default=".", help="Target git repository root.")
    bootstrap_parser.add_argument("--input-folder", default=None, help="Localization folder in the target repo.")
    bootstrap_parser.add_argument(
        "--localization-format",
        default="java_properties",
        help="Localization format for explicit --input-folder mode.",
    )
    bootstrap_parser.add_argument(
        "--localization-layout",
        default="suffix",
        help="Localization layout for explicit --input-folder mode.",
    )
    bootstrap_parser.add_argument("--source-locale", default="en", help="Source locale code.")
    bootstrap_parser.add_argument("--config-file", default="config.yaml", help="Config file to create.")
    bootstrap_parser.add_argument("--glossary-file", default="glossary.json", help="Glossary file to create.")
    bootstrap_parser.add_argument(
        "--workflow-file",
        default=".github/workflows/translate.yml",
        help="GitHub Actions workflow file to create.",
    )
    bootstrap_parser.add_argument("--branch", default="localize/onboarding", help="Onboarding branch name.")
    bootstrap_parser.add_argument("--base-branch", default=None, help="Optional base branch to check out first.")
    bootstrap_parser.add_argument("--action-ref", default="v0.1.0", help="Action ref to use in the generated workflow.")
    bootstrap_parser.add_argument(
        "--onboarding-guide-file",
        default="docs/localize-pipeline.md",
        help="Target-repo onboarding guide to create.",
    )
    bootstrap_parser.add_argument(
        "--skip-onboarding-guide",
        action="store_true",
        help="Do not create a target-repo onboarding guide.",
    )
    bootstrap_parser.add_argument(
        "--plugin-module",
        action="append",
        default=[],
        help="Plugin module to load in the generated GitHub Action workflow. Repeatable.",
    )
    bootstrap_parser.add_argument(
        "--plugin-install-command",
        default=None,
        help="Optional shell command that installs custom adapter dependencies in the generated workflow.",
    )
    bootstrap_parser.add_argument("--overwrite", action="store_true", help="Replace existing onboarding files.")
    bootstrap_parser.add_argument(
        "--reset-branch",
        action="store_true",
        help="Reset an existing onboarding branch instead of refusing to overwrite it.",
    )
    bootstrap_parser.add_argument("--push", action="store_true", help="Push the onboarding branch to origin.")
    bootstrap_parser.add_argument("--open-pr", action="store_true", help="Push and open the onboarding PR with gh.")
    bootstrap_parser.set_defaults(func=_cmd_bootstrap_pr)

    quality_parser = subparsers.add_parser(
        "quality-gate",
        help="Recompute the translation quality gate report for changed files.",
    )
    quality_parser.add_argument("--repo-root", required=True)
    quality_parser.add_argument("--input-folder", required=True)
    quality_parser.add_argument("--config", required=True)
    quality_parser.add_argument("--validation-summary", required=True)
    quality_parser.add_argument("--output-json", required=True)
    quality_parser.add_argument("--output-markdown", required=True)
    quality_parser.add_argument("--changed-files", nargs="+", required=True)
    quality_parser.add_argument("--audit-scope", choices=["changed", "all"], default=None)
    quality_parser.set_defaults(func=_cmd_quality_gate)

    memory_parser = subparsers.add_parser(
        "memory",
        help="Inspect, import, export, and promote translation-memory entries.",
    )
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)

    memory_stats = memory_subparsers.add_parser("stats", help="Print translation-memory statistics.")
    memory_stats.add_argument("--memory-file", default="logs/translation_memory.json", help="Memory file to inspect.")
    memory_stats.add_argument(
        "--output-format",
        choices=("text", "json"),
        default="text",
        help="Stats output format.",
    )
    memory_stats.set_defaults(func=_print_memory_stats)

    memory_export = memory_subparsers.add_parser("export", help="Export a translation-memory file.")
    memory_export.add_argument("--memory-file", default="logs/translation_memory.json", help="Memory file to export.")
    memory_export.add_argument("--output", required=True, help="Destination memory JSON file.")
    memory_export.set_defaults(func=_cmd_memory_export)

    memory_import = memory_subparsers.add_parser("import", help="Merge an exported memory into a memory file.")
    memory_import.add_argument("--memory-file", default="logs/translation_memory.json", help="Target memory file.")
    memory_import.add_argument("--input", required=True, help="Source memory JSON file to import.")
    memory_import.set_defaults(func=_cmd_memory_import)

    memory_promote = memory_subparsers.add_parser("promote", help="Record one reviewed translation-memory entry.")
    memory_promote.add_argument("--memory-file", default="logs/translation_memory.json", help="Target memory file.")
    memory_promote.add_argument("--source-text", required=True, help="Reviewed source text.")
    memory_promote.add_argument("--target-text", required=True, help="Reviewed target translation.")
    memory_promote.add_argument("--locale", required=True, help="Target locale code.")
    memory_promote.add_argument("--format-id", required=True, help="Localization format id.")
    memory_promote.set_defaults(func=_cmd_memory_promote)

    memory_suggest = memory_subparsers.add_parser(
        "suggest",
        help="Show fuzzy memory candidates for human review without automatic reuse.",
    )
    memory_suggest.add_argument("--memory-file", default="logs/translation_memory.json", help="Memory file.")
    memory_suggest.add_argument("--source-text", required=True, help="Source text to match.")
    memory_suggest.add_argument("--locale", required=True, help="Target locale code.")
    memory_suggest.add_argument("--format-id", required=True, help="Localization format id.")
    memory_suggest.add_argument("--min-score", type=float, default=0.72, help="Minimum fuzzy match score.")
    memory_suggest.add_argument("--limit", type=_non_negative_int, default=5, help="Maximum suggestions to print.")
    memory_suggest.set_defaults(func=_cmd_memory_suggest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        plugin_args, command_argv = _extract_plugin_args(raw_argv)
    except ValueError as exc:
        parser.error(str(exc))
    args, forwarded_args = parser.parse_known_args(command_argv)
    if args.command == "init":
        args.init_args = forwarded_args
    elif forwarded_args:
        parser.error(f"unrecognized arguments: {' '.join(forwarded_args)}")
    load_plugins([*plugin_args, *args.plugin])
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
