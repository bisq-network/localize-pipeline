"""Exact-match translation memory with conflict-safe reuse.

The memory is intentionally append-oriented and may grow without bound in long
running projects. Add a prune/compaction command before using it as a shared
large-scale store.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Mapping


logger = logging.getLogger(__name__)


def normalize_memory_source(value: str | None) -> str:
    """Normalize source text before hashing for stable exact-match reuse."""
    if value is None:
        return ""
    return value.replace("\\n", "<newline>").replace("\n", "<newline>")


def memory_source_hash(value: str | None) -> str:
    """Return the stable translation-memory hash for a source string."""
    return hashlib.sha256(normalize_memory_source(value).encode("utf-8")).hexdigest()


def _entry_key(source_text: str, locale: str, format_id: str) -> str:
    return f"{format_id}:{locale}:{memory_source_hash(source_text)}"


@dataclass(frozen=True)
class TranslationMemoryStats:
    """Aggregate counts for one translation memory store."""

    total_entries: int
    active_entries: int
    conflict_entries: int
    locales: tuple[str, ...]
    formats: tuple[str, ...]


@dataclass(frozen=True)
class TranslationMemoryMergeResult:
    """Result of merging an imported memory into a target memory."""

    imported_entries: int = 0
    unchanged_entries: int = 0
    conflict_entries: int = 0


@dataclass(frozen=True)
class TranslationMemorySuggestion:
    """A fuzzy translation-memory candidate for human review."""

    source_text: str
    target_text: str
    locale: str
    format_id: str
    score: float


@dataclass
class TranslationMemory:
    """In-memory representation of reusable approved translations."""

    entries: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def lookup(self, source_text: str, *, locale: str, format_id: str) -> str | None:
        """Return a reusable translation, or None when absent/ambiguous."""
        entry = self.entries.get(_entry_key(source_text, locale, format_id))
        if not entry or entry.get("status") == "conflict":
            return None
        target = entry.get("target")
        return str(target) if target is not None else None

    def record(self, source_text: str, target_text: str, *, locale: str, format_id: str) -> None:
        """Record one approved translation, marking conflicting targets unsafe."""
        key = _entry_key(source_text, locale, format_id)
        source_hash = memory_source_hash(source_text)
        existing = self.entries.get(key)
        if not existing:
            self.entries[key] = {
                "source": source_text,
                "source_hash": source_hash,
                "locale": locale,
                "format_id": format_id,
                "target": target_text,
                "status": "active",
            }
            return

        if existing.get("status") == "conflict":
            targets = set(str(target) for target in existing.get("targets", []))
            targets.add(target_text)
            existing.setdefault("source", source_text)
            if len(targets) == 1:
                _demote_conflict(existing, next(iter(targets)))
                return
            existing["targets"] = sorted(targets)
            return

        if existing.get("target") == target_text:
            existing.setdefault("source", source_text)
            return

        previous_target = str(existing.pop("target", ""))
        existing["status"] = "conflict"
        existing["targets"] = sorted({previous_target, target_text} - {""})
        existing.setdefault("source", source_text)

    def stats(self) -> TranslationMemoryStats:
        """Return high-level counts for observability and onboarding checks."""
        active_entries = 0
        conflict_entries = 0
        locales: set[str] = set()
        formats: set[str] = set()
        for entry in self.entries.values():
            status = entry.get("status", "active")
            if status == "conflict":
                conflict_entries += 1
            else:
                active_entries += 1
            locale = entry.get("locale")
            format_id = entry.get("format_id")
            if locale:
                locales.add(str(locale))
            if format_id:
                formats.add(str(format_id))
        return TranslationMemoryStats(
            total_entries=len(self.entries),
            active_entries=active_entries,
            conflict_entries=conflict_entries,
            locales=tuple(sorted(locales)),
            formats=tuple(sorted(formats)),
        )

    def merge_from(self, other: "TranslationMemory") -> TranslationMemoryMergeResult:
        """Merge another memory into this one, marking disagreements unsafe."""
        imported = 0
        unchanged = 0
        conflicts = 0
        for key, incoming in other.entries.items():
            existing = self.entries.get(key)
            incoming_copy = deepcopy(incoming)
            if existing is None:
                self.entries[key] = incoming_copy
                imported += 1
                continue

            if _entries_match(existing, incoming):
                _fill_missing_metadata(existing, incoming)
                unchanged += 1
                continue

            targets = _entry_targets(existing) | _entry_targets(incoming)
            if len(targets) <= 1:
                _fill_missing_metadata(existing, incoming)
                if targets and existing.get("status") == "conflict":
                    _demote_conflict(existing, next(iter(targets)))
                unchanged += 1
                continue

            _mark_conflict(existing, incoming, targets)
            conflicts += 1

        return TranslationMemoryMergeResult(
            imported_entries=imported,
            unchanged_entries=unchanged,
            conflict_entries=conflicts,
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "entries": self.entries,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TranslationMemory":
        entries = payload.get("entries", {})
        if not isinstance(entries, Mapping):
            return cls()
        valid_entries = {
            str(key): dict(value)
            for key, value in entries.items()
            if isinstance(value, Mapping)
        }
        return cls(entries=valid_entries)


def _entry_targets(entry: Mapping[str, Any]) -> set[str]:
    if entry.get("status") == "conflict":
        return {str(target) for target in entry.get("targets", []) if target is not None}
    target = entry.get("target")
    return {str(target)} if target is not None else set()


def _entries_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        left.get("status", "active") == right.get("status", "active")
        and _entry_targets(left) == _entry_targets(right)
    )


def _fill_missing_metadata(target: Dict[str, Any], source: Mapping[str, Any]) -> None:
    for key in ("source", "source_hash", "locale", "format_id"):
        if key not in target and key in source:
            target[key] = source[key]


def _mark_conflict(target: Dict[str, Any], source: Mapping[str, Any], targets: set[str]) -> None:
    target.pop("target", None)
    target["status"] = "conflict"
    target["targets"] = sorted(targets)
    _fill_missing_metadata(target, source)


def _demote_conflict(target: Dict[str, Any], value: str) -> None:
    target["status"] = "active"
    target["target"] = value
    target.pop("targets", None)


def merge_translation_memory(
    target: TranslationMemory,
    incoming: TranslationMemory,
) -> TranslationMemoryMergeResult:
    """Merge incoming approved translations into target memory."""
    return target.merge_from(incoming)


def translation_memory_suggestions(
    memory: TranslationMemory,
    source_text: str,
    *,
    locale: str,
    format_id: str,
    min_score: float = 0.72,
    limit: int = 5,
) -> tuple[TranslationMemorySuggestion, ...]:
    """Return fuzzy candidates for human review without automatic reuse."""
    normalized_source = normalize_memory_source(source_text)
    suggestions: list[TranslationMemorySuggestion] = []
    for entry in memory.entries.values():
        if entry.get("status", "active") == "conflict":
            continue
        if entry.get("locale") != locale or entry.get("format_id") != format_id:
            continue
        entry_source = entry.get("source")
        target_text = entry.get("target")
        if target_text is None:
            continue
        if entry_source is None:
            if entry.get("source_hash") != memory_source_hash(source_text):
                continue
            entry_source = source_text
            score = 1.0
        else:
            score = SequenceMatcher(
                None,
                normalized_source,
                normalize_memory_source(str(entry_source)),
            ).ratio()
        if score >= min_score:
            suggestions.append(
                TranslationMemorySuggestion(
                    source_text=str(entry_source),
                    target_text=str(target_text),
                    locale=str(locale),
                    format_id=str(format_id),
                    score=score,
                )
            )
    suggestions.sort(key=lambda candidate: candidate.score, reverse=True)
    return tuple(suggestions[:limit])


def _payload_with_timestamp(memory: TranslationMemory) -> Dict[str, Any]:
    payload = memory.to_payload()
    payload["updated_at"] = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")
    return payload


def write_translation_memory(path: str | Path, memory: TranslationMemory) -> None:
    """Persist translation memory atomically, raising write errors."""
    memory_path = Path(path)
    temp_path = memory_path.with_suffix(f"{memory_path.suffix}.tmp")
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_text(
        json.dumps(_payload_with_timestamp(memory), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(memory_path)


def load_translation_memory(path: str | Path) -> TranslationMemory:
    """Load translation memory from disk, returning empty memory on absence/error."""
    memory_path = Path(path)
    if not memory_path.exists():
        return TranslationMemory()
    try:
        payload = json.loads(memory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TranslationMemory()
    if not isinstance(payload, Mapping):
        return TranslationMemory()
    return TranslationMemory.from_payload(payload)


def load_translation_memory_strict(
    path: str | Path,
    *,
    require_exists: bool = False,
) -> TranslationMemory:
    """Load memory for explicit file-management commands, raising on bad input."""
    memory_path = Path(path)
    if not memory_path.exists():
        if require_exists:
            raise FileNotFoundError(f"translation memory file not found: {memory_path}")
        return TranslationMemory()
    try:
        payload = json.loads(memory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid translation memory file: {memory_path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid translation memory file: {memory_path}")
    entries = payload.get("entries", {})
    if not isinstance(entries, Mapping):
        raise ValueError(f"invalid translation memory entries in: {memory_path}")
    return TranslationMemory.from_payload(payload)


def save_translation_memory(path: str | Path, memory: TranslationMemory) -> None:
    """Persist translation memory atomically."""
    memory_path = Path(path)
    temp_path = memory_path.with_suffix(f"{memory_path.suffix}.tmp")
    try:
        write_translation_memory(memory_path, memory)
    except Exception as exc:
        logger.warning("Could not save translation memory to '%s': %s", memory_path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
