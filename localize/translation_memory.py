"""Exact-match translation memory with conflict-safe reuse."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
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
            existing["targets"] = sorted(targets)
            return

        if existing.get("target") == target_text:
            return

        previous_target = str(existing.pop("target", ""))
        existing["status"] = "conflict"
        existing["targets"] = sorted({previous_target, target_text} - {""})

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


def save_translation_memory(path: str | Path, memory: TranslationMemory) -> None:
    """Persist translation memory atomically."""
    memory_path = Path(path)
    temp_path = memory_path.with_suffix(f"{memory_path.suffix}.tmp")
    try:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        payload = memory.to_payload()
        payload["updated_at"] = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(memory_path)
    except OSError as exc:
        logger.warning("Could not save translation memory to '%s': %s", memory_path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
