"""Apply safe semantic-review suggestions to localization files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from localize.localization_adapters import get_localization_adapter
from localize.localization_profiles import LocalizationProfile
from localize.translation_validator import (
    check_placeholder_parity,
    find_disallowed_control_characters,
)


@dataclass
class SemanticRemediationResult:
    """Summary of semantic review suggestions applied to files."""

    applied: list[Dict[str, str]] = field(default_factory=list)
    skipped: list[Dict[str, str]] = field(default_factory=list)

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def applied_identities(self) -> set[tuple[str, str]]:
        return {
            (item["file"], item["key"])
            for item in self.applied
        }


def _input_folder_path(repo_root: str, input_folder: str) -> Path:
    path = Path(input_folder).expanduser()
    if path.is_absolute():
        return path
    return (Path(repo_root).expanduser() / path).resolve()


def _finding_file_path(input_folder: Path, file_name: str) -> Path:
    path = Path(file_name)
    if path.is_absolute():
        return path
    return input_folder / path


def _relative_to_input(path: Path, input_folder: Path) -> str:
    try:
        return path.relative_to(input_folder).as_posix()
    except ValueError:
        return os.path.relpath(path, input_folder)


def _matching_profile(
    relative_file: str,
    locale_codes: Sequence[str],
    localization_profiles: Sequence[LocalizationProfile],
) -> LocalizationProfile | None:
    for profile in localization_profiles:
        if not profile.localization_format.is_supported_file(relative_file):
            continue
        if profile.localization_layout.is_target_file(
            relative_file,
            locale_codes,
            profile.localization_format,
        ):
            return profile
    return None


def _skip(result: SemanticRemediationResult, finding: Mapping[str, Any], reason: str) -> None:
    result.skipped.append({
        "file": str(finding.get("file", "")),
        "key": str(finding.get("key", "")),
        "reason": reason,
    })


def apply_semantic_review_suggestions(
    *,
    repo_root: str,
    input_folder: str,
    findings: Sequence[Mapping[str, Any]],
    locale_codes: Sequence[str],
    localization_profiles: Sequence[LocalizationProfile],
) -> SemanticRemediationResult:
    """Apply error-level AI review suggestions that pass deterministic checks.

    Suggestions are intentionally conservative: the finding must name a changed
    target file/key, provide ``suggested_value``, map to a configured format
    profile, and preserve source placeholder parity.
    """
    result = SemanticRemediationResult()
    input_path = _input_folder_path(repo_root, input_folder)

    for finding in findings:
        if str(finding.get("severity", "")).lower() != "error":
            _skip(result, finding, "only error-level findings are auto-applied")
            continue
        suggested_value = finding.get("suggested_value")
        if not isinstance(suggested_value, str) or not suggested_value:
            _skip(result, finding, "missing suggested_value")
            continue
        file_name = str(finding.get("file") or "")
        key = str(finding.get("key") or "")
        if not file_name or not key:
            _skip(result, finding, "missing file or key")
            continue

        target_path = _finding_file_path(input_path, file_name)
        if not target_path.exists():
            _skip(result, finding, "target file not found")
            continue
        relative_file = _relative_to_input(target_path, input_path)
        profile = _matching_profile(relative_file, locale_codes, localization_profiles)
        if profile is None:
            _skip(result, finding, "no configured localization profile matched the target file")
            continue

        adapter = get_localization_adapter(profile.localization_format)
        parsed_lines, target_translations = adapter.parse_file(str(target_path))
        if key not in target_translations:
            _skip(result, finding, "key not found in target file")
            continue

        source_rel = profile.localization_layout.source_path_for_target(
            relative_file,
            locale_codes,
            profile.localization_format,
        )
        source_path = input_path / source_rel
        if not source_path.exists():
            _skip(result, finding, "source file not found")
            continue
        _, source_translations = adapter.parse_file(str(source_path))
        source_value = source_translations.get(key)
        if source_value is None:
            _skip(result, finding, "source key not found")
            continue

        escaped_value = adapter.escape_translation(source_value, suggested_value)
        if find_disallowed_control_characters(escaped_value):
            _skip(result, finding, "suggestion contains disallowed control characters")
            continue
        if not check_placeholder_parity(source_value, escaped_value):
            _skip(result, finding, "suggestion does not preserve source placeholders")
            continue

        for line in parsed_lines:
            if line.get("type") == "entry" and line.get("key") == key:
                line["value"] = escaped_value
                break
        target_path.write_text(adapter.reassemble_file(parsed_lines), encoding="utf-8")
        result.applied.append({
            "file": relative_file,
            "key": key,
            "suggested_value": escaped_value,
            "source": str(finding.get("source", "ai-review")),
        })

    return result
