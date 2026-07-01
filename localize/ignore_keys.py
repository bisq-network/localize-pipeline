"""Helpers for configured translation-key exclusion patterns."""

from __future__ import annotations

import re
from re import Pattern
from typing import Iterable, Sequence


def compile_ignore_key_patterns(raw_patterns: Iterable[str | Pattern[str]] | None) -> list[Pattern[str]]:
    """Compile configured ignore-key regexes once at config load.

    Patterns are matched against adapter-produced translation keys. JSON adapters
    produce JSON Pointer keys such as ``/#1`` or ``/nav/settings``; Java
    properties adapters produce the dotted property key.
    """
    if raw_patterns is None:
        return []

    compiled: list[Pattern[str]] = []
    for raw_pattern in raw_patterns:
        if isinstance(raw_pattern, Pattern):
            compiled.append(raw_pattern)
            continue
        if not isinstance(raw_pattern, str):
            raise ValueError(
                "ignore_key_patterns entries must be strings or compiled regex patterns; "
                f"got {type(raw_pattern).__name__}."
            )
        try:
            compiled.append(re.compile(raw_pattern))
        except re.error as exc:
            raise ValueError(
                f"Invalid ignore_key_patterns regex {raw_pattern!r}: {exc}. "
                "Fix or remove this pattern from config.yaml."
            ) from exc
    return compiled


def is_ignored_key(key: str, patterns: Sequence[Pattern[str]]) -> bool:
    """Return true when ``key`` matches at least one ignore pattern."""
    return any(pattern.search(key) for pattern in patterns)
