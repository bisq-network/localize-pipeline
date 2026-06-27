"""Reusable conformance checks for localization format adapters."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Set

from localize.localization_adapters import LocalizationFileAdapter


@dataclass(frozen=True)
class LocalizationAdapterConformanceCase:
    """Representative source/target fixture for one adapter contract check."""

    source_content: str
    target_content: str
    expected_source_translations: Dict[str, str]
    expected_target_translations: Dict[str, str]
    expected_added_keys: Set[str] = field(default_factory=set)
    expected_deleted_keys: Set[str] = field(default_factory=set)
    changed_diff_line: str = ""
    expected_changed_key: Optional[str] = None
    review_keys: Sequence[str] = field(default_factory=tuple)
    expected_review_fragment: str = ""
    escape_source_value: str = ""
    escape_translation_value: str = ""
    expected_escaped_translation: str = ""


def assert_localization_adapter_conformance(
    adapter: LocalizationFileAdapter,
    case: LocalizationAdapterConformanceCase,
) -> None:
    """Assert that an adapter satisfies the public parser/serializer contract.

    The helper intentionally uses plain assertions and temporary files so adapter
    authors can call it from any pytest suite without depending on project
    internals.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        source_path = os.path.join(tmp_dir, f"source{adapter.localization_format.file_extension}")
        target_path = os.path.join(tmp_dir, f"target{adapter.localization_format.file_extension}")
        with open(source_path, "w", encoding="utf-8") as file:
            file.write(case.source_content)
        with open(target_path, "w", encoding="utf-8") as file:
            file.write(case.target_content)

        source_lines, source_translations = adapter.parse_file(source_path)
        target_lines, target_translations = adapter.parse_file(target_path)

        assert source_translations == case.expected_source_translations
        assert target_translations == case.expected_target_translations
        assert adapter.lint_file(source_path) == []

        round_trip_path = os.path.join(tmp_dir, f"round-trip{adapter.localization_format.file_extension}")
        with open(round_trip_path, "w", encoding="utf-8") as file:
            file.write(adapter.reassemble_file(source_lines))
        _round_trip_lines, round_trip_translations = adapter.parse_file(round_trip_path)
        assert round_trip_translations == case.expected_source_translations

        if case.review_keys:
            review_content = adapter.build_review_content(target_translations, case.review_keys)
            assert case.expected_review_fragment in review_content

        if case.changed_diff_line:
            assert adapter.extract_changed_key_from_diff_line(
                case.changed_diff_line
            ) == case.expected_changed_key

        if case.expected_escaped_translation:
            assert adapter.escape_translation(
                case.escape_source_value,
                case.escape_translation_value,
            ) == case.expected_escaped_translation

        added_keys, deleted_keys = adapter.synchronize_keys(target_path, source_path)
        assert added_keys == case.expected_added_keys
        assert deleted_keys == case.expected_deleted_keys

        _synced_lines, synced_translations = adapter.parse_file(target_path)
        for key in case.expected_added_keys:
            assert synced_translations[key] == case.expected_source_translations[key]
