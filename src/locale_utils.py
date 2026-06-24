"""Shared helpers for recognising locale-suffixed ``.properties`` filenames.

Single source of truth for the locale-suffix pattern used by both the change
detector (``translate_localization_files``) and the init scaffolder
(``init_config``), so the rules for what counts as a translation file cannot
drift between them.
"""

from __future__ import annotations

import re
from typing import Optional

# A locale suffix: lowercase language (2-3 letters) plus an optional region/script
# segment, e.g. ``_de``, ``_pt_BR``, ``-Hant``. The language is captured as group 1.
LOCALE_SUFFIX_RE = re.compile(r"_([a-z]{2,3}(?:[-_][A-Za-z]{2,4})?)\.properties$")


def extract_locale_suffix(filename: str) -> Optional[str]:
    """Return the locale code from a filename, or ``None`` if it has no suffix."""
    match = LOCALE_SUFFIX_RE.search(filename)
    return match.group(1) if match else None


def is_locale_file(filename: str) -> bool:
    """True when ``filename`` is a locale-suffixed ``.properties`` file."""
    return LOCALE_SUFFIX_RE.search(filename) is not None
