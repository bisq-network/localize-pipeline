"""Apply safe semantic-review suggestions to localization files."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from localize.localization_adapters import get_localization_adapter
from localize.localization_profiles import LocalizationProfile
from localize.placeholder_rules import PLACEHOLDER_PATTERN
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
    def applied_finding_signatures(self) -> set[str]:
        return {
            item["finding_signature"]
            for item in self.applied
            if item.get("finding_signature")
        }


def _input_folder_path(repo_root: str, input_folder: str) -> Path:
    path = Path(input_folder).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path(repo_root).expanduser() / path).resolve()


def _finding_file_path(input_folder: Path, file_name: str) -> Path:
    path = Path(file_name)
    if path.is_absolute():
        return path
    return input_folder / path


def _resolve_within(base: Path, candidate: Path) -> Path | None:
    resolved_base = base.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_base)
    except ValueError:
        return None
    return resolved_candidate


def semantic_review_finding_signature(
    finding: Mapping[str, Any],
    *,
    file_name: str | None = None,
    key: str | None = None,
) -> str:
    """Return a stable identity for one concrete semantic-review finding."""
    return json.dumps(
        [
            file_name if file_name is not None else str(finding.get("file", "")),
            key if key is not None else str(finding.get("key", "")),
            str(finding.get("severity", "")),
            str(finding.get("reason", "")),
            str(finding.get("suggested_value", "")),
            str(finding.get("source", "")),
            str(finding.get("rule_id", "")),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


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


def _numeric_literals(value: str) -> set[str]:
    without_placeholders = PLACEHOLDER_PATTERN.sub("", value)
    return {
        "".join(str(unicodedata.decimal(character)) for character in match.group())
        for match in re.finditer(r"\d+", without_placeholders)
    }


def apply_semantic_review_suggestions(
    *,
    repo_root: str,
    input_folder: str,
    findings: Sequence[Mapping[str, Any]],
    locale_codes: Sequence[str],
    localization_profiles: Sequence[LocalizationProfile],
    changed_identities: Iterable[tuple[str, str]],
) -> SemanticRemediationResult:
    """Apply error-level AI review suggestions that pass deterministic checks.

    Suggestions are intentionally conservative: the finding must name a changed
    target file/key, provide ``suggested_value``, map to a configured format
    profile, preserve source placeholder parity, and avoid introducing numeric
    requirements that are absent from the authoritative source.
    """
    result = SemanticRemediationResult()
    input_path = _input_folder_path(repo_root, input_folder)
    changed_identity_set = set(changed_identities)
    seen_candidate_identities: set[tuple[str, str]] = set()

    for finding in findings:
        if str(finding.get("severity", "")).lower() != "error":
            _skip(result, finding, "only error-level findings are auto-applied")
            continue
        suggested_value = finding.get("suggested_value")
        if not isinstance(suggested_value, str) or not suggested_value:
            _skip(result, finding, "missing suggested_value")
            continue
        if any(ch in suggested_value for ch in ("\r", "\n")):
            _skip(result, finding, "suggestion contains line break characters")
            continue
        file_name = str(finding.get("file") or "")
        key = str(finding.get("key") or "")
        if not file_name or not key:
            _skip(result, finding, "missing file or key")
            continue

        target_path = _resolve_within(input_path, _finding_file_path(input_path, file_name))
        if target_path is None:
            _skip(result, finding, "target file is outside input_folder")
            continue
        if not target_path.exists():
            _skip(result, finding, "target file not found")
            continue
        relative_file = target_path.relative_to(input_path).as_posix()
        identity = (relative_file, key)
        if identity not in changed_identity_set:
            _skip(result, finding, "finding is not scoped to a changed entry")
            continue
        if identity in seen_candidate_identities:
            _skip(result, finding, "duplicate finding for changed entry")
            continue

        profile = _matching_profile(relative_file, locale_codes, localization_profiles)
        if profile is None:
            _skip(result, finding, "no configured localization profile matched the target file")
            continue

        adapter = get_localization_adapter(profile.localization_format)
        try:
            parsed_lines, target_translations = adapter.parse_file(str(target_path))
        except Exception as exc:
            _skip(result, finding, f"target file could not be parsed: {exc}")
            continue
        if key not in target_translations:
            _skip(result, finding, "key not found in target file")
            continue

        source_rel = profile.localization_layout.source_path_for_target(
            relative_file,
            locale_codes,
            profile.localization_format,
        )
        source_path = _resolve_within(input_path, input_path / source_rel)
        if source_path is None:
            _skip(result, finding, "source file is outside input_folder")
            continue
        if not source_path.exists():
            _skip(result, finding, "source file not found")
            continue
        try:
            _, source_translations = adapter.parse_file(str(source_path))
        except Exception as exc:
            _skip(result, finding, f"source file could not be parsed: {exc}")
            continue
        source_value = source_translations.get(key)
        if source_value is None:
            _skip(result, finding, "source key not found")
            continue
        if not _numeric_literals(suggested_value).issubset(_numeric_literals(source_value)):
            _skip(result, finding, "suggestion introduces numeric literals absent from source")
            continue

        escaped_value = adapter.escape_translation(source_value, suggested_value)
        if find_disallowed_control_characters(escaped_value):
            _skip(result, finding, "suggestion contains disallowed control characters")
            continue
        if not check_placeholder_parity(source_value, escaped_value):
            _skip(result, finding, "suggestion does not preserve source placeholders")
            continue

        for line in reversed(parsed_lines):
            if line.get("type") == "entry" and line.get("key") == key:
                line["value"] = escaped_value
                break
        temp_path = target_path.with_name(f".{target_path.name}.tmp")
        temp_path.write_text(adapter.reassemble_file(parsed_lines), encoding="utf-8")
        temp_path.replace(target_path)
        seen_candidate_identities.add(identity)
        result.applied.append({
            "file": relative_file,
            "key": key,
            "severity": "error",
            "reason": str(finding.get("reason", "")),
            "value": str(finding.get("value", "")),
            "rule_id": str(finding.get("rule_id", "")),
            "suggested_value": escaped_value,
            "source": str(finding.get("source", "ai-review")),
            "finding_signature": semantic_review_finding_signature(
                finding,
                file_name=relative_file,
                key=key,
            ),
        })

    return result
