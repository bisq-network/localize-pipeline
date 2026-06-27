"""Localization file layout metadata.

Formats describe file contents. Layouts describe where locale files live in a
repository. Keeping those concerns separate lets one JSON adapter work for both
``messages_de.json`` and ``locales/de/messages.json`` projects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Dict, Mapping, Optional, Sequence

from localize.localization_formats import LocalizationFormat


def _as_posix(path: str) -> str:
    return PurePath(path.replace("\\", "/")).as_posix()


def _path_parts(path: str) -> tuple[str, ...]:
    return PurePath(_as_posix(path)).parts


@dataclass(frozen=True)
class LocalizationLayout:
    """Path convention for source and target locale files."""

    id: str
    source_locale: str = "en"

    def __post_init__(self) -> None:
        if self.id not in {"suffix", "locale_directory", "locale_filename"}:
            raise ValueError(
                "Unsupported localization_layout "
                f"'{self.id}'. Supported ids: locale_directory, locale_filename, suffix."
            )
        if not self.source_locale:
            raise ValueError("localization_layout.source_locale must not be empty.")

    def extract_locale(
        self,
        relative_path: str,
        supported_codes: Sequence[str],
        localization_format: LocalizationFormat,
    ) -> Optional[str]:
        """Return the target locale code encoded in ``relative_path``."""
        path = _as_posix(relative_path)
        supported = sorted((str(code) for code in supported_codes), key=len, reverse=True)
        if self.id == "suffix":
            return localization_format.extract_supported_locale_suffix(PurePath(path).name, list(supported))
        if self.id == "locale_directory":
            parts = _path_parts(path)
            return next((part for part in supported if part in parts), None)
        if self.id == "locale_filename":
            stem = PurePath(path).stem
            return next((code for code in supported if stem == code), None)
        return None

    def is_target_file(
        self,
        relative_path: str,
        supported_codes: Sequence[str],
        localization_format: LocalizationFormat,
    ) -> bool:
        """Return true when ``relative_path`` is a translatable target locale file."""
        if not localization_format.is_supported_file(relative_path):
            return False
        locale = self.extract_locale(relative_path, supported_codes, localization_format)
        return bool(locale and locale != self.source_locale)

    def is_source_file(
        self,
        relative_path: str,
        supported_codes: Sequence[str],
        localization_format: LocalizationFormat,
    ) -> bool:
        """Return true when ``relative_path`` is a source-language localization file."""
        path = _as_posix(relative_path)
        if not localization_format.is_supported_file(path):
            return False
        if self.id == "suffix":
            return localization_format.extract_locale_suffix(PurePath(path).name) is None
        if self.id == "locale_directory":
            return self.source_locale in _path_parts(path)
        if self.id == "locale_filename":
            return PurePath(path).stem == self.source_locale
        return False

    def source_path_for_target(
        self,
        relative_path: str,
        supported_codes: Sequence[str],
        localization_format: LocalizationFormat,
    ) -> str:
        """Return the source-locale path corresponding to a target locale path."""
        path = _as_posix(relative_path)
        pure_path = PurePath(path)
        if self.id == "suffix":
            source_name = localization_format.source_filename(pure_path.name, list(supported_codes))
            return pure_path.with_name(source_name).as_posix()
        if self.id == "locale_directory":
            locale = self.extract_locale(path, supported_codes, localization_format)
            if not locale:
                return path
            parts = list(_path_parts(path))
            parts[parts.index(locale)] = self.source_locale
            return PurePath(*parts).as_posix()
        if self.id == "locale_filename":
            return pure_path.with_name(f"{self.source_locale}{localization_format.file_extension}").as_posix()
        return path


SUFFIX_LAYOUT = LocalizationLayout(id="suffix")
LOCALE_DIRECTORY_LAYOUT = LocalizationLayout(id="locale_directory")
LOCALE_FILENAME_LAYOUT = LocalizationLayout(id="locale_filename")

_LAYOUT_REGISTRY: Dict[str, LocalizationLayout] = {
    SUFFIX_LAYOUT.id: SUFFIX_LAYOUT,
    LOCALE_DIRECTORY_LAYOUT.id: LOCALE_DIRECTORY_LAYOUT,
    LOCALE_FILENAME_LAYOUT.id: LOCALE_FILENAME_LAYOUT,
}


def load_localization_layout(raw_value: Any, *, source_locale: str = "en") -> LocalizationLayout:
    """Build a ``LocalizationLayout`` from config."""
    if raw_value in (None, ""):
        return LocalizationLayout(id=SUFFIX_LAYOUT.id, source_locale=source_locale)

    if isinstance(raw_value, str):
        base = _LAYOUT_REGISTRY.get(raw_value)
        if not base:
            supported = ", ".join(sorted(_LAYOUT_REGISTRY))
            raise ValueError(
                f"Unsupported localization_layout '{raw_value}'. Supported registry ids: {supported}."
            )
        return LocalizationLayout(id=base.id, source_locale=source_locale)

    if isinstance(raw_value, Mapping):
        layout_id = str(raw_value.get("id") or SUFFIX_LAYOUT.id)
        return LocalizationLayout(
            id=layout_id,
            source_locale=str(raw_value.get("source_locale") or source_locale),
        )

    raise ValueError("localization_layout must be a string id or mapping.")
