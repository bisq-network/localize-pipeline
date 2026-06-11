"""Quality gate for generated translation pull requests."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from src.properties_parser import parse_properties_file
from src.semantic_quality import (
    SemanticFinding,
    SemanticQAStats,
    SemanticRule,
    analyze_all_translation_entries,
    analyze_translation_changes,
    iter_added_properties_entries as _iter_added_properties_entries,
    iter_translation_changes_from_diff,
    load_semantic_rules,
    normalize_value,
    source_filename_for_locale_file,
)
from src.translation_validator import find_disallowed_control_characters


@dataclass
class QualityGateConfig:
    """Thresholds for blocking generated translation pull requests."""

    source_identical_min_block_count: int = 5
    source_identical_max_count: int = 20
    source_identical_max_ratio: float = 0.30
    block_on_pipeline_warnings: bool = True
    block_on_semantic_qa_findings: bool = True
    block_on_semantic_qa_warnings: bool = False
    semantic_qa_audit_scope: str = "changed"


@dataclass
class SourceIdenticalStats:
    changed_entries_count: int = 0
    checked_entries_count: int = 0
    source_identical_count: int = 0
    expected_source_identical_count: int = 0
    unexpected_source_identical_count: int = 0
    unexpected_source_identical_ratio: float = 0.0
    control_character_findings_count: int = 0
    examples: List[Dict[str, str]] = None
    control_character_examples: List[Dict[str, str]] = None

    def __post_init__(self) -> None:
        if self.examples is None:
            self.examples = []
        if self.control_character_examples is None:
            self.control_character_examples = []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_TOKEN_PATTERNS = [
    re.compile(r"^\{[^{}]+\}$"),
    re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE),
    re.compile(r"^\S+@\S+$"),
    re.compile(r"^[A-Z0-9_.:+/#-]{2,}$"),
]
_ENUM_LIKE_KEY = re.compile(r"^[A-Z0-9_.$-]+$")


def is_expected_source_identical(key: str, value: str, brand_glossary: Iterable[str]) -> bool:
    """Return true for values that are commonly and legitimately untranslated."""
    normalized = normalize_value(value)
    if not normalized:
        return True
    if _ENUM_LIKE_KEY.match(key):
        return True
    glossary = {term.strip().casefold() for term in brand_glossary if str(term).strip()}
    if normalized.casefold() in glossary:
        return True
    if not any(character.isalpha() for character in normalized):
        return True
    return any(pattern.match(normalized) for pattern in _TOKEN_PATTERNS)


def _relpath(path: str, start: str) -> str:
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return path


def analyze_source_identical_changes(
    diff_text: str,
    repo_root: str,
    input_folder: str,
    locale_codes: Sequence[str],
    brand_glossary: Iterable[str],
    examples_limit: int = 10,
) -> SourceIdenticalStats:
    """Analyze staged translation changes for suspicious English-source fallbacks."""
    repo_root_path = Path(repo_root)
    input_folder_path = Path(input_folder)
    source_cache: Dict[Path, Dict[str, str]] = {}
    stats = SourceIdenticalStats()

    for changed_file, key, target_value in _iter_added_properties_entries(diff_text):
        changed_path = repo_root_path / changed_file
        source_name = source_filename_for_locale_file(changed_path.name, locale_codes)
        if not source_name:
            continue

        source_path = changed_path.with_name(source_name)
        if not source_path.exists():
            continue

        if source_path not in source_cache:
            _, source_translations = parse_properties_file(str(source_path))
            source_cache[source_path] = source_translations
        else:
            source_translations = source_cache[source_path]

        if key not in source_translations:
            continue

        stats.changed_entries_count += 1

        control_findings = find_disallowed_control_characters(target_value)
        if control_findings:
            stats.control_character_findings_count += len(control_findings)
            if len(stats.control_character_examples) < examples_limit:
                stats.control_character_examples.append(
                    {
                        "file": _relpath(str(changed_path), str(input_folder_path)),
                        "key": key,
                        "findings": ", ".join(control_findings[:3]),
                    }
                )

        source_value = source_translations[key]
        if normalize_value(source_value) != normalize_value(target_value):
            stats.checked_entries_count += 1
            continue

        stats.source_identical_count += 1
        if is_expected_source_identical(key, source_value, brand_glossary):
            stats.expected_source_identical_count += 1
            continue

        stats.checked_entries_count += 1
        stats.unexpected_source_identical_count += 1
        if len(stats.examples) < examples_limit:
            stats.examples.append(
                {
                    "file": _relpath(str(changed_path), str(input_folder_path)),
                    "key": key,
                    "value": target_value,
                }
            )

    if stats.checked_entries_count:
        stats.unexpected_source_identical_ratio = (
            stats.unexpected_source_identical_count / stats.checked_entries_count
        )
    return stats


def analyze_semantic_qa_changes(
    diff_text: str,
    repo_root: str,
    input_folder: str,
    locale_codes: Sequence[str],
    brand_glossary: Iterable[str] = (),
    semantic_rules: Sequence[SemanticRule] = (),
    examples_limit: int = 10,
) -> SemanticQAStats:
    """Scan changed translations for configured semantic regressions."""
    changes = list(
        iter_translation_changes_from_diff(
            diff_text=diff_text,
            repo_root=repo_root,
            input_folder=input_folder,
            locale_codes=locale_codes,
        )
    )
    return analyze_translation_changes(
        changes=changes,
        semantic_rules=semantic_rules,
        brand_glossary=brand_glossary,
        examples_limit=examples_limit,
    )


def load_quality_gate_config(
    config_path: str,
) -> Tuple[QualityGateConfig, List[str], List[str], List[SemanticRule]]:
    with open(config_path, "r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    quality_gate = raw_config.get("quality_gate", {}) or {}
    locales = [
        locale["code"]
        for locale in raw_config.get("supported_locales", [])
        if isinstance(locale, dict) and locale.get("code")
    ]
    brand_glossary = [str(term) for term in raw_config.get("brand_technical_glossary", [])]
    return (
        QualityGateConfig(
            source_identical_min_block_count=int(
                quality_gate.get("source_identical_min_block_count", 5)
            ),
            source_identical_max_count=int(quality_gate.get("source_identical_max_count", 20)),
            source_identical_max_ratio=float(quality_gate.get("source_identical_max_ratio", 0.30)),
            block_on_pipeline_warnings=bool(quality_gate.get("block_on_pipeline_warnings", True)),
            block_on_semantic_qa_findings=bool(
                quality_gate.get("block_on_semantic_qa_findings", True)
            ),
            block_on_semantic_qa_warnings=bool(
                quality_gate.get("block_on_semantic_qa_warnings", False)
            ),
            semantic_qa_audit_scope=str(
                quality_gate.get("semantic_qa_audit_scope", "changed")
            ),
        ),
        locales,
        brand_glossary,
        load_semantic_rules(raw_config.get("semantic_quality_rules", [])),
    )


def load_validation_summary(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"files": {}, "pipeline_warnings": []}
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        return {"files": {}, "pipeline_warnings": []}
    data.setdefault("files", {})
    data.setdefault("pipeline_warnings", [])
    return data


def _changed_files_relative_to_input(changed_files: Sequence[str], input_folder: str) -> set[str]:
    input_path = Path(input_folder)
    result = set()
    for changed_file in changed_files:
        changed_path = Path(changed_file)
        result.add(changed_path.as_posix())
        result.add(changed_path.name)
        if changed_path.is_absolute():
            try:
                result.add(changed_path.relative_to(input_path).as_posix())
            except ValueError:
                result.add(changed_path.name)
        else:
            input_folder_name = input_path.name
            parts = changed_path.parts
            for index, part in enumerate(parts):
                if part == input_folder_name and index + 1 < len(parts):
                    result.add(Path(*parts[index + 1 :]).as_posix())
    return result


def _file_matches_changed_files(filename: str, changed_relative_files: set[str]) -> bool:
    path = Path(filename)
    return path.as_posix() in changed_relative_files or path.name in changed_relative_files


def _aggregate_validation(
    validation_summary: Dict[str, Any],
    changed_files: Sequence[str],
    input_folder: str,
) -> Dict[str, int]:
    changed_relative_files = _changed_files_relative_to_input(changed_files, input_folder)
    totals = {
        "reverted_keys_count": 0,
        "control_character_findings_count": 0,
        "placeholder_failures_count": 0,
    }
    for filename, file_summary in validation_summary.get("files", {}).items():
        if changed_relative_files and not _file_matches_changed_files(filename, changed_relative_files):
            continue
        totals["reverted_keys_count"] += int(file_summary.get("reverted_keys_count", 0))
        totals["control_character_findings_count"] += int(
            file_summary.get("control_character_findings_count", 0)
        )
        totals["placeholder_failures_count"] += int(file_summary.get("placeholder_failures_count", 0))
    return totals


def _filter_pipeline_warnings(
    validation_summary: Dict[str, Any],
    changed_files: Sequence[str],
    input_folder: str,
) -> List[Dict[str, Any]]:
    changed_relative_files = _changed_files_relative_to_input(changed_files, input_folder)
    warnings = validation_summary.get("pipeline_warnings", [])
    if not changed_relative_files:
        return list(warnings)
    return [
        warning
        for warning in warnings
        if _file_matches_changed_files(str(warning.get("file", "")), changed_relative_files)
    ]


def _semantic_review_stats_from_validation(
    validation_summary: Dict[str, Any],
    changed_files: Sequence[str],
    input_folder: str,
) -> SemanticQAStats:
    changed_relative_files = _changed_files_relative_to_input(changed_files, input_folder)
    findings: List[SemanticFinding] = []
    for raw_finding in validation_summary.get("semantic_review_findings", []):
        if not isinstance(raw_finding, dict):
            continue
        filename = str(raw_finding.get("file", ""))
        if changed_relative_files and not _file_matches_changed_files(filename, changed_relative_files):
            continue
        severity = str(raw_finding.get("severity", "warning")).lower()
        if severity not in {"error", "warning"}:
            severity = "warning"
        findings.append(
            SemanticFinding(
                file=filename,
                key=str(raw_finding.get("key", "")),
                value=str(raw_finding.get("value", raw_finding.get("new_value", ""))),
                reason=str(raw_finding.get("reason", "")),
                severity=severity,
                rule_id=str(raw_finding.get("rule_id", "ai-review")),
                source=str(raw_finding.get("source", "ai-review")),
                suggested_value=(
                    str(raw_finding["suggested_value"])
                    if raw_finding.get("suggested_value")
                    else None
                ),
            )
        )
    return SemanticQAStats.from_findings(findings)


def _merge_semantic_stats(
    first: Optional[SemanticQAStats],
    second: Optional[SemanticQAStats],
    examples_limit: int = 10,
) -> SemanticQAStats:
    first = first or SemanticQAStats()
    second = second or SemanticQAStats()
    return SemanticQAStats(
        findings_count=first.findings_count + second.findings_count,
        errors_count=first.errors_count + second.errors_count,
        warnings_count=first.warnings_count + second.warnings_count,
        examples=[*first.examples, *second.examples][:examples_limit],
    )


def build_quality_gate_report(
    source_stats: SourceIdenticalStats,
    semantic_stats: Optional[SemanticQAStats],
    validation_summary: Dict[str, Any],
    changed_files: Sequence[str],
    input_folder: str,
    config: QualityGateConfig,
) -> Dict[str, Any]:
    validation_totals = _aggregate_validation(validation_summary, changed_files, input_folder)
    pipeline_warnings = _filter_pipeline_warnings(validation_summary, changed_files, input_folder)
    blocking_reasons: List[str] = []
    semantic_stats = _merge_semantic_stats(
        semantic_stats,
        _semantic_review_stats_from_validation(validation_summary, changed_files, input_folder),
    )

    source_identical_blocking = (
        source_stats.unexpected_source_identical_count >= config.source_identical_min_block_count
        and (
            source_stats.unexpected_source_identical_count > config.source_identical_max_count
            or source_stats.unexpected_source_identical_ratio > config.source_identical_max_ratio
        )
    )
    if source_identical_blocking:
        blocking_reasons.append(
            "Unexpected source-identical changed values exceed configured quality thresholds."
        )

    if config.block_on_pipeline_warnings and pipeline_warnings:
        blocking_reasons.append("Translation pipeline warnings require manual resolution.")

    if config.block_on_semantic_qa_findings and semantic_stats.errors_count:
        blocking_reasons.append("Semantic translation QA findings require manual resolution.")
    elif config.block_on_semantic_qa_warnings and semantic_stats.warnings_count:
        blocking_reasons.append("Semantic translation QA warnings require manual resolution.")

    blocking = bool(blocking_reasons)
    description = (
        blocking_reasons[0]
        if blocking
        else "Translation quality gate passed."
    )
    description = description[:140]

    return {
        "blocking": blocking,
        "blocking_reasons": blocking_reasons,
        "status_state": "failure" if blocking else "success",
        "status_description": description,
        "source_identical": source_stats.to_dict(),
        "semantic_qa": semantic_stats.to_dict(),
        "validation": validation_totals,
        "pipeline_warnings_count": len(pipeline_warnings),
        "pipeline_warnings": pipeline_warnings,
        "thresholds": asdict(config),
    }


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _truncate(value: str, limit: int = 100) -> str:
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 3] + "..."


def render_quality_gate_markdown(report: Dict[str, Any]) -> str:
    source = report["source_identical"]
    semantic_qa = report.get("semantic_qa", {"findings_count": 0, "examples": []})
    validation = report["validation"]
    lines = ["## Translation Validation Summary", ""]

    if report["blocking"]:
        lines.append("**Status:** Blocked by `translation-quality-gate`.")
        lines.append("")
        for reason in report["blocking_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
    else:
        lines.append("**Status:** Passed `translation-quality-gate`.")
        lines.append("")

    lines.extend(
        [
            "| Check | Result |",
            "| --- | --- |",
            (
                "| Unexpected source-identical changed values | "
                f"{source['unexpected_source_identical_count']} / "
                f"{source['checked_entries_count']} checked "
                f"({_format_percent(source['unexpected_source_identical_ratio'])}) |"
            ),
            (
                "| Expected source-identical values ignored | "
                f"{source['expected_source_identical_count']} |"
            ),
            (
                "| Semantic QA findings | "
                f"{semantic_qa['findings_count']} "
                f"({semantic_qa.get('errors_count', 0)} errors, "
                f"{semantic_qa.get('warnings_count', 0)} warnings) |"
            ),
            f"| Reverted keys | {validation['reverted_keys_count']} |",
            f"| Control-character findings | {validation['control_character_findings_count']} |",
            f"| Placeholder failures | {validation['placeholder_failures_count']} |",
            f"| Pipeline warnings | {report['pipeline_warnings_count']} |",
            "",
        ]
    )

    if source.get("examples"):
        lines.append("### Source-Identical Examples")
        lines.append("")
        for example in source["examples"]:
            lines.append(
                f"- `{example['file']}` `{example['key']}` = `{_truncate(example['value'])}`"
            )
        lines.append("")

    if semantic_qa.get("examples"):
        lines.append("### Semantic QA Examples")
        lines.append("")
        for example in semantic_qa["examples"]:
            lines.append(
                f"- `{example['file']}` `{example['key']}`: "
                f"{example['reason']} Value: `{_truncate(example['value'])}`"
            )
        lines.append("")

    if source.get("control_character_examples"):
        lines.append("### Control-Character Examples")
        lines.append("")
        for example in source["control_character_examples"]:
            lines.append(
                f"- `{example['file']}` `{example['key']}`: {example['findings']}"
            )
        lines.append("")

    if report["pipeline_warnings"]:
        lines.append("### Pipeline Warnings")
        lines.append("")
        for warning in report["pipeline_warnings"]:
            lines.append(f"- `{warning.get('file', 'unknown')}`")
            for error in warning.get("errors", []):
                lines.append(f"  - {_truncate(str(error), 180)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def get_staged_diff(repo_root: str, changed_files: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--", *changed_files],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def write_report(report: Dict[str, Any], output_json: str, output_markdown: str) -> None:
    Path(output_json).parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    with open(output_markdown, "w", encoding="utf-8") as file:
        file.write(render_quality_gate_markdown(report))


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--input-folder", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--validation-summary", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    parser.add_argument("--changed-files", nargs="+", required=True)
    parser.add_argument("--audit-scope", choices=["changed", "all"], default=None)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    config, locale_codes, brand_glossary, semantic_rules = load_quality_gate_config(args.config)
    diff_text = get_staged_diff(args.repo_root, args.changed_files)
    source_stats = analyze_source_identical_changes(
        diff_text=diff_text,
        repo_root=args.repo_root,
        input_folder=args.input_folder,
        locale_codes=locale_codes,
        brand_glossary=brand_glossary,
    )
    audit_scope = args.audit_scope or config.semantic_qa_audit_scope
    if audit_scope == "all":
        semantic_stats = analyze_all_translation_entries(
            repo_root=args.repo_root,
            input_folder=args.input_folder,
            locale_codes=locale_codes,
            brand_glossary=brand_glossary,
            semantic_rules=semantic_rules,
        )
    else:
        semantic_stats = analyze_semantic_qa_changes(
            diff_text=diff_text,
            repo_root=args.repo_root,
            input_folder=args.input_folder,
            locale_codes=locale_codes,
            brand_glossary=brand_glossary,
            semantic_rules=semantic_rules,
        )
    report = build_quality_gate_report(
        source_stats=source_stats,
        semantic_stats=semantic_stats,
        validation_summary=load_validation_summary(args.validation_summary),
        changed_files=args.changed_files,
        input_folder=args.input_folder,
        config=config,
    )
    write_report(report, args.output_json, args.output_markdown)
    return 1 if report["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
