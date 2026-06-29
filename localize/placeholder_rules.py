"""Reusable placeholder detection and protection rules."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Dict, Match, Tuple


_HTML_TAG = r"<[^<>]+>"
_I18NEXT_TOKEN = r"\{\{[^{}\n]+\}\}"
_BRACE_TOKEN = r"\{[A-Za-z0-9_][^{}\n]*\}"
_PYTHON_NAMED_PRINTF = r"%\([^)]+\)[#0 +\-]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z]"
_POSITIONAL_PRINTF = r"%(?:\d+\$)?[#0+\-]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z@]"

PLACEHOLDER_PATTERN = re.compile(
    "|".join(
        [
            _HTML_TAG,
            _I18NEXT_TOKEN,
            _BRACE_TOKEN,
            _PYTHON_NAMED_PRINTF,
            _POSITIONAL_PRINTF,
        ]
    )
)


def extract_placeholder_tokens(text: str) -> Counter[str]:
    """Return placeholder/tag tokens in ``text`` with multiplicity."""
    if not isinstance(text, str):
        raise ValueError("Input text must be a string.")
    return Counter(match.group(0) for match in PLACEHOLDER_PATTERN.finditer(text))


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

    return PLACEHOLDER_PATTERN.sub(replace_placeholder, text), placeholder_mapping


def restore_placeholders(text: str, placeholder_mapping: Dict[str, str]) -> str:
    """Restore placeholders previously replaced by ``protect_placeholders``."""
    if not text or not placeholder_mapping:
        return text
    for token, placeholder in placeholder_mapping.items():
        text = text.replace(token, placeholder)
    return text
