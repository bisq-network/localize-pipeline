"""Command line interface for the reusable localization pipeline."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from localize.app_config import ConfigIssue, validate_config
from localize.formats import list_localization_adapters, list_localization_formats
from localize.init_config import main as init_config_main
from localize.plugins import load_plugins


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
