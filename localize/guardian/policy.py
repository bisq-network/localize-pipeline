"""Deterministic value-only policy gate for guardian proposals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import yaml

from localize.formats import get_localization_adapter
from localize.guardian.models import ProposedReplacement
from localize.guardian.path_globs import matches_any_path_glob
from localize.localization_profiles import LocalizationProfile, load_localization_profiles
from localize.placeholder_rules import DEFAULT_PLACEHOLDER_PROFILE, placeholder_profile
from localize.translation_validator import (
    check_encoding_and_mojibake,
    check_placeholder_parity,
    find_disallowed_control_characters,
    find_glossary_mismatches,
)


class PatchPolicyError(ValueError):
    """Raised when a proposed replacement fails a deterministic policy gate."""


@dataclass(frozen=True)
class PatchResult:
    """The exact files and canonical keys changed by one validated batch."""

    changed_files: tuple[str, ...]
    changed_keys: tuple[tuple[str, str], ...]


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PatchPolicyError(f"Could not read pipeline config: {exc}") from exc
    if not isinstance(payload, dict):
        raise PatchPolicyError("Pipeline config must contain a YAML mapping.")
    return payload


def _locale_codes(config: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    raw_locales = config.get("supported_locales") or []
    if not isinstance(raw_locales, list):
        raise PatchPolicyError("supported_locales must be a list.")
    for item in raw_locales:
        if isinstance(item, str) and item:
            result.append(item)
        elif isinstance(item, Mapping) and item.get("code"):
            result.append(str(item["code"]))
    if not result:
        raise PatchPolicyError("Pipeline config has no supported target locales.")
    return tuple(dict.fromkeys(result))


def _safe_relative_path(raw_path: str, *, repo_root: Path) -> tuple[str, Path]:
    if "\\" in raw_path or "\x00" in raw_path:
        raise PatchPolicyError(
            f"Replacement path is not a safe repository-relative path: {raw_path!r}."
        )
    normalized = raw_path
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PatchPolicyError(f"Replacement path is not a safe repository-relative path: {raw_path!r}.")
    relative = pure.as_posix()
    unresolved_destination = repo_root / relative
    _reject_symlink_ancestors(
        unresolved_destination,
        root=repo_root,
        label="replacement path",
    )
    destination = unresolved_destination.resolve()
    try:
        destination.relative_to(repo_root)
    except ValueError as exc:
        raise PatchPolicyError(f"Replacement path escapes the repository: {raw_path!r}.") from exc
    return relative, destination


def _path_allowed(relative_path: str, allowed_paths: Sequence[str]) -> bool:
    return matches_any_path_glob(relative_path, allowed_paths)


def _profile_for_target(
    relative_path: str,
    profiles: Sequence[LocalizationProfile],
    locale_codes: Sequence[str],
) -> tuple[LocalizationProfile, str]:
    matches: list[tuple[LocalizationProfile, str]] = []
    for profile in profiles:
        if not profile.localization_layout.is_target_file(
            relative_path,
            locale_codes,
            profile.localization_format,
        ):
            continue
        locale = profile.localization_layout.extract_locale(
            relative_path,
            locale_codes,
            profile.localization_format,
        )
        if locale and locale != profile.localization_layout.source_locale:
            matches.append((profile, locale))
    if len(matches) != 1:
        raise PatchPolicyError(
            f"Path {relative_path!r} does not identify exactly one configured target locale."
        )
    return matches[0]


def _trusted_regular_file(path: Path, *, root: Path, label: str) -> Path:
    _reject_symlink_ancestors(path, root=root, label=f"trusted {label}")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PatchPolicyError(f"Trusted {label} is not a regular file: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PatchPolicyError(f"Trusted {label} must be a non-symlinked regular file.")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PatchPolicyError(f"Trusted {label} escapes its trusted base checkout.") from exc
    return resolved


def _reject_symlink_ancestors(path: Path, *, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PatchPolicyError(f"{label.capitalize()} escapes its trusted root.") from exc
    current = root
    for component in relative.parts:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PatchPolicyError(f"Could not inspect {label}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PatchPolicyError(f"{label.capitalize()} is a symbolic link.")


def _load_glossary(
    config: Mapping[str, Any],
    *,
    config_path: Path,
    trusted_root: Path,
) -> dict[str, dict[str, str]]:
    configured_path = config.get("glossary_file_path")
    explicitly_configured = configured_path is not None
    raw_path = configured_path if explicitly_configured else "glossary.json"
    raw_text = str(raw_path)
    pure = PurePosixPath(raw_text)
    if (
        not raw_text
        or "\\" in raw_text
        or "\x00" in raw_text
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise PatchPolicyError("Trusted glossary path must be a safe relative POSIX path.")
    path = config_path.parent.joinpath(*pure.parts)
    if not path.exists():
        if explicitly_configured or path.is_symlink():
            raise PatchPolicyError(
                "Trusted glossary is not a regular file: configured path is missing."
            )
        return {}
    path = _trusted_regular_file(path, root=trusted_root, label="glossary")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchPolicyError(f"Could not read glossary: {exc}") from exc
    if not isinstance(payload, dict):
        raise PatchPolicyError("Glossary must contain a JSON object.")
    result: dict[str, dict[str, str]] = {}
    for locale, mappings in payload.items():
        if str(locale).startswith("_"):
            continue
        if not isinstance(mappings, dict) or not all(
            isinstance(source, str) and isinstance(target, str)
            for source, target in mappings.items()
        ):
            raise PatchPolicyError(f"Glossary locale {locale!r} must map strings to strings.")
        result[str(locale)] = dict(mappings)
    return result


def _new_findings(*, adapter: Any, before_path: Path, after_path: Path) -> list[str]:
    before = set(adapter.lint_file(str(before_path))) | set(check_encoding_and_mojibake(str(before_path)))
    after = set(adapter.lint_file(str(after_path))) | set(check_encoding_and_mojibake(str(after_path)))
    return sorted(after - before)


def _write_preserving_mode(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".guardian",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _read_text_without_newline_conversion(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def apply_replacements(
    *,
    repo_root: Path,
    pipeline_config_path: Path,
    allowed_paths: Sequence[str],
    replacements: Iterable[ProposedReplacement],
    max_changes: int,
    trusted_config_root: Path | None = None,
    trusted_source_root: Path | None = None,
    expected_source_locale: str | None = None,
) -> PatchResult:
    """Validate a complete batch, then apply only the proposed entry values.

    No model-generated patch or filename is executed.  The controller resolves
    canonical keys through the configured localization adapter, verifies every
    invariant against the source locale, and computes every output before the
    first worktree write.
    """
    root = repo_root.expanduser().resolve()
    raw_config_path = pipeline_config_path.expanduser()
    if trusted_config_root is not None:
        trusted_root = trusted_config_root.expanduser().resolve()
        if not raw_config_path.is_absolute():
            raw_config_path = trusted_root / raw_config_path
    else:
        if not raw_config_path.is_absolute():
            raw_config_path = Path.cwd() / raw_config_path
        trusted_root = raw_config_path.parent.resolve()
    source_root = (
        trusted_source_root.expanduser().resolve()
        if trusted_source_root is not None
        else root
    )
    if not trusted_root.is_dir():
        raise PatchPolicyError("Trusted config root is not a directory.")
    config_path = _trusted_regular_file(
        raw_config_path,
        root=trusted_root,
        label="pipeline config",
    )
    proposals = tuple(replacements)
    if max_changes < 0 or len(proposals) > max_changes:
        raise PatchPolicyError(
            f"Replacement count {len(proposals)} exceeds the configured limit {max_changes}."
        )
    if not proposals:
        return PatchResult(changed_files=(), changed_keys=())
    if not allowed_paths:
        raise PatchPolicyError("No allowed path patterns are configured.")

    config = _load_yaml_mapping(config_path)
    locale_codes = _locale_codes(config)
    try:
        profiles = load_localization_profiles(config)
    except ValueError as exc:
        raise PatchPolicyError(f"Invalid localization profile: {exc}") from exc
    configured_source_locale = str(config.get("source_locale") or "en")
    if expected_source_locale is not None and (
        configured_source_locale != expected_source_locale
        or any(
            profile.localization_layout.source_locale != expected_source_locale
            for profile in profiles
        )
    ):
        raise PatchPolicyError(
            "Trusted pipeline config source locale does not match Guardian policy."
        )
    glossary = _load_glossary(
        config,
        config_path=config_path,
        trusted_root=trusted_root,
    )
    brand_terms = tuple(str(term) for term in (config.get("brand_technical_glossary") or ()))
    glossary_enforcement = str(config.get("translation_glossary_enforcement") or "exact").lower()
    active_placeholder_profile = str(
        config.get("placeholder_profile") or DEFAULT_PLACEHOLDER_PROFILE
    )

    grouped: dict[str, list[ProposedReplacement]] = defaultdict(list)
    destinations: dict[str, Path] = {}
    seen: set[tuple[str, str]] = set()
    for proposal in proposals:
        relative_path, destination = _safe_relative_path(proposal.path, repo_root=root)
        if not _path_allowed(relative_path, allowed_paths):
            raise PatchPolicyError(f"Path {relative_path!r} is outside every allowed path pattern.")
        identity = (relative_path, proposal.key)
        if identity in seen:
            raise PatchPolicyError(f"Replacement batch contains duplicate target {relative_path}:{proposal.key}.")
        seen.add(identity)
        if not destination.is_file():
            raise PatchPolicyError(f"Target localization file does not exist: {relative_path}.")
        grouped[relative_path].append(proposal)
        destinations[relative_path] = destination

    pending_content: dict[str, str] = {}
    changed_keys: list[tuple[str, str]] = []
    with placeholder_profile(active_placeholder_profile):
        for relative_path, path_proposals in grouped.items():
            profile, locale = _profile_for_target(relative_path, profiles, locale_codes)
            adapter = get_localization_adapter(profile.localization_format)
            source_relative = profile.localization_layout.source_path_for_target(
                relative_path,
                locale_codes,
                profile.localization_format,
            )
            source_path = _trusted_regular_file(
                source_root / source_relative,
                root=source_root,
                label=f"source localization file {source_relative!r}",
            )

            destination = destinations[relative_path]
            original_text = _read_text_without_newline_conversion(destination)
            parsed_lines, target_values = adapter.parse_file(str(destination))
            _source_lines, source_values = adapter.parse_file(str(source_path))
            if adapter.reassemble_file(parsed_lines) != original_text:
                raise PatchPolicyError(
                    f"Adapter round-trip for {relative_path!r} is not byte-quiet; refusing to rewrite it."
                )
            entries = {
                str(entry.get("key")): entry
                for entry in parsed_lines
                if entry.get("type") == "entry"
            }

            for proposal in path_proposals:
                if proposal.locale != locale:
                    raise PatchPolicyError(
                        f"Proposal locale {proposal.locale!r} does not match target locale {locale!r}."
                    )
                if proposal.key not in target_values or proposal.key not in entries:
                    raise PatchPolicyError(f"Unknown key {proposal.key!r} in {relative_path!r}.")
                if proposal.key not in source_values:
                    raise PatchPolicyError(f"Source file has no key {proposal.key!r}.")
                current_value = target_values[proposal.key]
                source_value = source_values[proposal.key]
                if current_value != proposal.expected_value:
                    raise PatchPolicyError(
                        f"The expected value for {relative_path}:{proposal.key} is stale."
                    )
                if proposal.source_value is not None and source_value != proposal.source_value:
                    raise PatchPolicyError(
                        f"The expected source value for {relative_path}:{proposal.key} is stale."
                    )
                if proposal.proposed_value == current_value:
                    raise PatchPolicyError(f"Proposal for {relative_path}:{proposal.key} is a no-op.")
                if find_disallowed_control_characters(proposal.proposed_value):
                    raise PatchPolicyError(
                        f"Proposed value for {relative_path}:{proposal.key} contains a control character."
                    )
                if not check_placeholder_parity(source_value, proposal.proposed_value):
                    raise PatchPolicyError(
                        f"Proposed value for {relative_path}:{proposal.key} breaks placeholder parity."
                    )
                locale_glossary = dict(glossary.get(locale, {}))
                locale_glossary.update({term: term for term in brand_terms})
                if glossary_enforcement == "exact":
                    mismatches = find_glossary_mismatches(
                        source_value,
                        proposal.proposed_value,
                        locale_glossary,
                    )
                    if mismatches:
                        raise PatchPolicyError(
                            f"Proposed value for {relative_path}:{proposal.key} violates glossary requirements."
                        )
                entries[proposal.key]["value"] = proposal.proposed_value
                changed_keys.append((relative_path, proposal.key))

            new_text = adapter.reassemble_file(parsed_lines)
            with tempfile.TemporaryDirectory(prefix="localize-guardian-") as temporary_dir:
                after_path = Path(temporary_dir) / destination.name
                after_path.write_text(new_text, encoding="utf-8", newline="")
                findings = _new_findings(
                    adapter=adapter,
                    before_path=destination,
                    after_path=after_path,
                )
                if findings:
                    raise PatchPolicyError(
                        f"Replacement introduces localization lint findings in {relative_path}: "
                        + "; ".join(findings)
                    )
                _roundtrip_lines, roundtrip_values = adapter.parse_file(str(after_path))
            changed = {
                key
                for key in set(target_values) | set(roundtrip_values)
                if target_values.get(key) != roundtrip_values.get(key)
            }
            expected_changed = {proposal.key for proposal in path_proposals}
            if changed != expected_changed:
                raise PatchPolicyError(
                    f"Adapter changed values outside the approved keys in {relative_path}."
                )
            pending_content[relative_path] = new_text

    for relative_path in sorted(pending_content):
        _write_preserving_mode(destinations[relative_path], pending_content[relative_path])

    return PatchResult(
        changed_files=tuple(sorted(pending_content)),
        changed_keys=tuple(sorted(changed_keys)),
    )
