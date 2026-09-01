"""Shared segment-aware path allowlist semantics."""

from __future__ import annotations

import pytest

from localize.guardian.path_globs import matches_path_glob


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("l10n/app.properties", "l10n/*.properties", True),
        ("l10n/nested/app.properties", "l10n/*.properties", False),
        ("localize/rules.py", "localize/**/*.py", True),
        ("localize/guardian/rules.py", "localize/**/*.py", True),
        ("tests/test_rules.py", "**/*.py", True),
        ("tests/unit/test_rules.py", "**/*.py", True),
        ("apps/i18n/messages.properties", "apps/**/i18n/**", True),
        ("apps/desktop/i18n/messages.properties", "apps/**/i18n/**", True),
        ("apps/desktop/src/messages.properties", "apps/**/i18n/**", False),
        ("src/x", "src/?", True),
        ("src/xy", "src/?", False),
        ("src/nested/x", "src/**", True),
    ],
)
def test_segment_aware_path_globs(
    path: str,
    pattern: str,
    expected: bool,
) -> None:
    assert matches_path_glob(path, pattern) is expected


@pytest.mark.parametrize(
    "path",
    ["", "/absolute/file.py", "../file.py", "a/../file.py", "a//file.py", "a\\file.py"],
)
def test_path_globs_reject_noncanonical_repository_paths(path: str) -> None:
    assert matches_path_glob(path, "**") is False
