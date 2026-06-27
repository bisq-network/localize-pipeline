"""Configured localization format/layout profiles.

Formats describe file contents; layouts describe where locale files live. A
project can use more than one pair, e.g. Java properties with suffix filenames
and JSON files in locale directories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from localize.localization_formats import LocalizationFormat, load_localization_format
from localize.localization_layouts import LocalizationLayout, load_localization_layout


@dataclass(frozen=True)
class LocalizationProfile:
    """One configured localization format and path layout pair."""

    localization_format: LocalizationFormat
    localization_layout: LocalizationLayout


def _profile_format_raw(raw_profile: Any) -> Any:
    if isinstance(raw_profile, str):
        return raw_profile
    if isinstance(raw_profile, Mapping):
        if any(
            key in raw_profile
            for key in ("display_name", "file_extension", "code_fence", "locale_suffix_regex")
        ):
            return raw_profile
        return (
            raw_profile.get("format")
            or raw_profile.get("localization_format")
            or raw_profile.get("id")
        )
    return raw_profile


def _profile_layout_raw(raw_profile: Any, fallback_layout: Any) -> Any:
    if isinstance(raw_profile, Mapping):
        return raw_profile.get("layout") or raw_profile.get("localization_layout") or fallback_layout
    return fallback_layout


def _profile_source_locale(raw_profile: Any, fallback_source_locale: str) -> str:
    if isinstance(raw_profile, Mapping):
        return str(raw_profile.get("source_locale") or fallback_source_locale)
    return fallback_source_locale


def _load_profile(
    raw_profile: Any,
    *,
    fallback_layout: Any,
    fallback_source_locale: str,
) -> LocalizationProfile:
    localization_format = load_localization_format(_profile_format_raw(raw_profile))
    localization_layout = load_localization_layout(
        _profile_layout_raw(raw_profile, fallback_layout),
        source_locale=_profile_source_locale(raw_profile, fallback_source_locale),
    )
    return LocalizationProfile(
        localization_format=localization_format,
        localization_layout=localization_layout,
    )


def load_localization_profiles(config: Mapping[str, Any]) -> Tuple[LocalizationProfile, ...]:
    """Load one or more localization format/layout profiles from config.

    Backward compatibility:
    - ``localization_format`` + ``localization_layout`` still define a single
      profile.

    Multi-format projects can use:
    ``localization_formats: [{id: json, layout: locale_directory}, ...]``.
    """
    fallback_source_locale = str(config.get("source_locale") or "en")
    fallback_layout = config.get("localization_layout")
    raw_profiles = config.get("localization_formats")

    if raw_profiles is None:
        return (
            _load_profile(
                config.get("localization_format"),
                fallback_layout=fallback_layout,
                fallback_source_locale=fallback_source_locale,
            ),
        )

    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("localization_formats must contain at least one profile.")

    return tuple(
        _load_profile(
            raw_profile,
            fallback_layout=fallback_layout,
            fallback_source_locale=fallback_source_locale,
        )
        for raw_profile in raw_profiles
    )
