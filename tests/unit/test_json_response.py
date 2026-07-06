"""Tests for provider-neutral JSON response helpers."""

import pytest

from localize.json_response import extract_json_object_text, loads_json_object


def test_extract_json_object_text_accepts_fenced_json():
    assert extract_json_object_text('```json\n{"ok": true}\n```') == '{"ok": true}'


def test_extract_json_object_text_accepts_unfenced_json_with_prefix():
    assert extract_json_object_text('Result:\n{"ok": true}\nThanks') == '{"ok": true}'


def test_extract_json_object_text_handles_nested_objects():
    assert extract_json_object_text('{"outer": {"inner": 1}, "ok": true}') == (
        '{"outer": {"inner": 1}, "ok": true}'
    )


def test_extract_json_object_text_ignores_braces_inside_strings():
    response = '{"message": "literal { brace } text", "ok": true} trailing'

    assert extract_json_object_text(response) == (
        '{"message": "literal { brace } text", "ok": true}'
    )


def test_extract_json_object_text_accepts_unterminated_fence_with_balanced_json():
    assert extract_json_object_text('```json\n{"ok": true}') == '{"ok": true}'


def test_extract_json_object_text_returns_original_text_when_no_object_exists():
    assert extract_json_object_text("not json") == "not json"


def test_loads_json_object_rejects_non_object_json():
    with pytest.raises(ValueError, match="Expected a JSON object response"):
        loads_json_object("[1, 2, 3]")
