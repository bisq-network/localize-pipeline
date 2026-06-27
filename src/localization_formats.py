"""Localization format metadata and filename conventions.

The runtime currently translates Java ``.properties`` files, but the rest of the
pipeline should not need to know that every filename ends in ``.properties`` or
that locale suffixes use ``_<locale>``. This module keeps that knowledge in one
place so future adapters can be registered without spreading format checks
through the translation pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


_BCP47_LOCALE_CODE = r"[a-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*"


@dataclass(frozen=True)
class LocalizationFormat:
    """Metadata and filename helpers for one localization file format."""

    id: str
    display_name: str
    file_extension: str
    code_fence: str
    locale_suffix_regex: str
    _compiled_regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Localization format id must not be empty.")
        if not self.file_extension.startswith("."):
            raise ValueError("Localization format file_extension must start with '.'.")
        compiled = re.compile(self.locale_suffix_regex)
        if compiled.groups < 1 and "locale" not in compiled.groupindex:
            raise ValueError("locale_suffix_regex must capture the locale as group 1 or named group 'locale'.")
        object.__setattr__(self, "_compiled_regex", compiled)

    @property
    def compiled_locale_suffix_regex(self) -> re.Pattern[str]:
        """Return the cached locale-suffix regex for this format."""
        return self._compiled_regex

    @property
    def _compiled_locale_suffix_regex(self) -> re.Pattern[str]:
        return self.compiled_locale_suffix_regex

    def is_supported_file(self, filename: str) -> bool:
        """Return true when ``filename`` uses this format's file extension."""
        return filename.endswith(self.file_extension)

    def extract_locale_suffix(self, filename: str) -> Optional[str]:
        """Return the locale code from ``filename``, or ``None`` if absent."""
        match = self._compiled_locale_suffix_regex.search(filename)
        if not match:
            return None
        if "locale" in match.groupdict():
            return match.group("locale")
        return match.group(1)

    def is_locale_file(self, filename: str) -> bool:
        """Return true when ``filename`` is a target-locale file."""
        return self.extract_locale_suffix(filename) is not None

    def extract_supported_locale_suffix(self, filename: str, supported_codes: list[str]) -> Optional[str]:
        """Return a supported locale code from ``filename`` using exact suffix matching first."""
        for code in sorted(supported_codes, key=len, reverse=True):
            for separator in ("_", ".", "-"):
                if filename.endswith(f"{separator}{code}{self.file_extension}"):
                    return code

        locale = self.extract_locale_suffix(filename)
        if locale and locale in supported_codes:
            return locale
        return None

    def source_filename(self, translation_file: str, supported_codes: list[str]) -> str:
        """Return the source filename corresponding to ``translation_file``."""
        for code in sorted(supported_codes, key=len, reverse=True):
            for separator in ("_", ".", "-"):
                suffix = f"{separator}{code}{self.file_extension}"
                if translation_file.endswith(suffix):
                    return translation_file[:-len(suffix)] + self.file_extension

        match = self._compiled_locale_suffix_regex.search(translation_file)
        if not match:
            return translation_file

        locale_group: str | int = "locale" if "locale" in match.groupdict() else 1
        locale = match.group(locale_group)
        if supported_codes and locale not in supported_codes:
            return translation_file

        locale_start = match.start(locale_group)
        separator_start = locale_start
        if locale_start > 0 and translation_file[locale_start - 1] in ("_", ".", "-"):
            separator_start = locale_start - 1
        elif match.start() < locale_start:
            separator_start = match.start()

        return translation_file[:separator_start] + self.file_extension


JAVA_PROPERTIES_FORMAT = LocalizationFormat(
    id="java_properties",
    display_name="Java .properties",
    file_extension=".properties",
    code_fence="properties",
    locale_suffix_regex=rf".*_({_BCP47_LOCALE_CODE})\.properties$",
)

JSON_FORMAT = LocalizationFormat(
    id="json",
    display_name="JSON",
    file_extension=".json",
    code_fence="json",
    locale_suffix_regex=rf".*[_.-]({_BCP47_LOCALE_CODE})\.json$",
)

_FORMAT_REGISTRY: Dict[str, LocalizationFormat] = {
    JAVA_PROPERTIES_FORMAT.id: JAVA_PROPERTIES_FORMAT,
    JSON_FORMAT.id: JSON_FORMAT,
}


def load_localization_format(raw_value: Any) -> LocalizationFormat:
    """Build a ``LocalizationFormat`` from config."""
    if raw_value in (None, ""):
        return JAVA_PROPERTIES_FORMAT

    if isinstance(raw_value, str):
        try:
            return _FORMAT_REGISTRY[raw_value]
        except KeyError as exc:
            supported = ", ".join(sorted(_FORMAT_REGISTRY))
            raise ValueError(
                f"Unsupported localization_format '{raw_value}'. Supported registry ids: {supported}."
            ) from exc

    if isinstance(raw_value, Mapping):
        base = _FORMAT_REGISTRY.get(str(raw_value.get("id", "")))
        values: Dict[str, Any] = {
            "id": raw_value.get("id") or (base.id if base else ""),
            "display_name": raw_value.get("display_name") or (base.display_name if base else ""),
            "file_extension": raw_value.get("file_extension") or (base.file_extension if base else ""),
            "code_fence": raw_value.get("code_fence") or (base.code_fence if base else ""),
            "locale_suffix_regex": raw_value.get("locale_suffix_regex")
            or (base.locale_suffix_regex if base else ""),
        }
        return LocalizationFormat(**values)

    raise ValueError("localization_format must be a string id or mapping.")
