"""Canonical, segment-aware POSIX path globs for Guardian allowlists."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import PurePosixPath
import re


def _canonical_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value != "."
        and "//" not in value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    if not _canonical_path(pattern):
        return None
    expression: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression.append("(?:[^/]+/)*")
                    index += 1
                else:
                    expression.append(".*")
                continue
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.compile("".join(expression))


def matches_path_glob(path: str, pattern: str) -> bool:
    """Return whether one canonical repository path matches one path glob."""

    if not _canonical_path(path):
        return False
    compiled = _compiled(pattern)
    return compiled is not None and compiled.fullmatch(path) is not None


def matches_any_path_glob(path: str, patterns: Sequence[str]) -> bool:
    """Return whether one path matches any segment-aware allowlist pattern."""

    return any(matches_path_glob(path, pattern) for pattern in patterns)


__all__ = ("matches_any_path_glob", "matches_path_glob")
