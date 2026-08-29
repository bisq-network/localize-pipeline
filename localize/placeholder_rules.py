"""Reusable placeholder detection and protection rules."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Iterator, Match, Tuple


_HTML_TAG = r"</?[A-Za-z][^<>\n]*>"
_PLACEHOLDER_TAG = r"<\{[A-Za-z0-9_][^{}\n]*\}[^<>\n]*>"
_I18NEXT_TOKEN = r"\{\{[^{}\n]+\}\}"
_BRACE_TOKEN = r"\{[A-Za-z0-9_][^{}\n]*\}"
_PYTHON_NAMED_PRINTF = r"%\([^)]+\)[#0 +\-]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z]"
_POSITIONAL_PRINTF = r"%(?!%)(?:\d+\$)?[#0+\-,(<]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[bBhHsScCdoxXeEfgGaAtT%n]"
# Java-style indexed placeholders (`%0`, `%1`, …) used by some localization
# libraries. Ordered after the printf alternatives so `%(name)s` and
# `%1$s` keep winning. Opt-in via the "java-indexed" placeholder profile only:
# percent-then-digit prose (fee/price copy like "5%0") cannot be ruled out in
# other projects, so this must never be on globally.
_JAVA_INDEXED_PRINTF = r"%\d+"
# In `%0%1`, the positional-printf rule would otherwise consume `%0%` as one
# token. This lookahead lets the Java profile protect each adjacent indexed
# placeholder without changing the standard profile's established behavior.
_JAVA_INDEXED_ADJACENT = r"%\d+(?=%\d)"

_STANDARD_ALTERNATIVES = [
    _PLACEHOLDER_TAG,
    _HTML_TAG,
    _I18NEXT_TOKEN,
    _BRACE_TOKEN,
    _PYTHON_NAMED_PRINTF,
    _POSITIONAL_PRINTF,
]

_PROFILE_PATTERNS = {
    "standard": re.compile("|".join(_STANDARD_ALTERNATIVES)),
    "java-indexed": re.compile("|".join(
        _STANDARD_ALTERNATIVES[:-1]
        + [_JAVA_INDEXED_ADJACENT, _POSITIONAL_PRINTF, _JAVA_INDEXED_PRINTF]
    )),
}

DEFAULT_PLACEHOLDER_PROFILE = "standard"

# Backward-compatible alias; always the standard-profile pattern. Internal
# helpers consult _active_pattern() instead so the profile switch applies.
PLACEHOLDER_PATTERN = _PROFILE_PATTERNS[DEFAULT_PLACEHOLDER_PROFILE]

_ACTIVE_PROFILE: ContextVar[str] = ContextVar(
    "placeholder_profile",
    default=DEFAULT_PLACEHOLDER_PROFILE,
)


def validate_placeholder_profile(profile_name: str) -> None:
    """Raise when ``profile_name`` is not a registered placeholder profile."""
    if profile_name not in _PROFILE_PATTERNS:
        valid_profiles = ", ".join(sorted(_PROFILE_PATTERNS))
        raise ValueError(
            f"Unknown placeholder profile '{profile_name}'. Valid profiles: {valid_profiles}."
        )


def set_placeholder_profile(profile_name: str) -> str:
    """Select the placeholder-detection profile; returns the previous profile.

    Valid profiles: "standard" (default, Bisq behavior) and "java-indexed"
    (adds `%N` tokens for projects that use indexed percent placeholders).
    Runtime entry points call this once after loading config; tests use
    placeholder_profile() instead.
    """
    validate_placeholder_profile(profile_name)
    previous_profile = _ACTIVE_PROFILE.get()
    _ACTIVE_PROFILE.set(profile_name)
    return previous_profile


@contextmanager
def placeholder_profile(profile_name: str) -> Iterator[None]:
    """Temporarily activate a placeholder profile (restores the previous one)."""
    validate_placeholder_profile(profile_name)
    context_token = _ACTIVE_PROFILE.set(profile_name)
    try:
        yield
    finally:
        _ACTIVE_PROFILE.reset(context_token)


def _active_pattern() -> re.Pattern[str]:
    return _PROFILE_PATTERNS[_ACTIVE_PROFILE.get()]


def extract_placeholder_tokens(text: str) -> Counter[str]:
    """Return placeholder/tag tokens in ``text`` with multiplicity."""
    if not isinstance(text, str):
        raise ValueError("Input text must be a string.")
    return Counter(match.group(0) for match in _active_pattern().finditer(text))


def strip_placeholder_tokens(text: str) -> str:
    """Remove tokens recognized by the active placeholder profile."""
    if not isinstance(text, str):
        raise ValueError("Input text must be a string.")
    return _active_pattern().sub("", text)


def protect_placeholders(text: str) -> Tuple[str, Dict[str, str]]:
    """Replace detected placeholders with opaque tokens and return the mapping."""
    if not isinstance(text, str):
        raise ValueError("Input text must be a string.")
    if not text:
        return "", {}

    placeholder_mapping: Dict[str, str] = {}

    def replace_placeholder(match: Match[str]) -> str:
        full_match = match.group(0)
        placeholder_token = f"__PH_{uuid.uuid4().hex}__"
        placeholder_mapping[placeholder_token] = full_match
        return placeholder_token

    return _active_pattern().sub(replace_placeholder, text), placeholder_mapping


def restore_placeholders(text: str, placeholder_mapping: Dict[str, str]) -> str:
    """Restore placeholders previously replaced by ``protect_placeholders``."""
    if not text:
        return text
    if placeholder_mapping:
        for token, placeholder in placeholder_mapping.items():
            text = text.replace(token, placeholder)
    if "__PH_" in text:
        raise ValueError("Unresolved placeholder protection token remains in text.")
    return text
