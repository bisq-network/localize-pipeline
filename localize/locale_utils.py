"""Backward-compatible helpers for Java properties locale filenames.

New code should prefer ``localize.localization_formats.LocalizationFormat`` directly.
These functions preserve the old import surface for Java ``.properties`` files.
"""

from __future__ import annotations

from typing import Optional

from localize.localization_formats import JAVA_PROPERTIES_FORMAT

LOCALE_SUFFIX_RE = JAVA_PROPERTIES_FORMAT.compiled_locale_suffix_regex


def extract_locale_suffix(filename: str) -> Optional[str]:
    """Return the locale code from a filename, or ``None`` if it has no suffix."""
    return JAVA_PROPERTIES_FORMAT.extract_locale_suffix(filename)


def is_locale_file(filename: str) -> bool:
    """True when ``filename`` is a locale-suffixed ``.properties`` file."""
    return JAVA_PROPERTIES_FORMAT.is_locale_file(filename)
