"""Tests for the JSON localization format adapter."""

import json

from src.localization_adapters import get_localization_adapter
from src.localization_formats import JSON_FORMAT


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_json_adapter_parses_string_leaves_as_json_pointer_keys(tmp_path):
    file_path = tmp_path / "messages.json"
    write_json(
        file_path,
        {
            "flat.key": "Flat value {0}",
            "nested": {
                "title": "Title",
                "count": 3,
            },
            "enabled": True,
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    parsed_lines, translations = adapter.parse_file(str(file_path))

    assert translations == {
        "/flat.key": "Flat value {0}",
        "/nested/title": "Title",
    }
    assert [line["key"] for line in parsed_lines if line["type"] == "entry"] == [
        "/flat.key",
        "/nested/title",
    ]


def test_json_adapter_parses_arrays_and_escapes_pointer_segments(tmp_path):
    file_path = tmp_path / "messages.json"
    write_json(
        file_path,
        {
            "steps": [
                {"title": "First"},
                {"title": "Second"},
            ],
            "literal/slash": {
                "tilde~key": "Escaped",
            },
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    parsed_lines, translations = adapter.parse_file(str(file_path))

    assert translations == {
        "/steps/0/title": "First",
        "/steps/1/title": "Second",
        "/literal~1slash/tilde~0key": "Escaped",
    }
    assert [line["key"] for line in parsed_lines if line["type"] == "entry"] == [
        "/steps/0/title",
        "/steps/1/title",
        "/literal~1slash/tilde~0key",
    ]


def test_json_adapter_reassembles_nested_values_without_touching_non_strings(tmp_path):
    file_path = tmp_path / "messages.json"
    write_json(
        file_path,
        {
            "welcome": "Welcome",
            "nested": {"title": "Title"},
            "count": 3,
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    parsed_lines, _translations = adapter.parse_file(str(file_path))
    for line in parsed_lines:
        if line.get("key") == "/nested/title":
            line["value"] = "Titel"

    reassembled = adapter.reassemble_file(parsed_lines)

    assert json.loads(reassembled) == {
        "welcome": "Welcome",
        "nested": {"title": "Titel"},
        "count": 3,
    }
    assert reassembled.endswith("\n")


def test_json_adapter_reassembles_array_values(tmp_path):
    file_path = tmp_path / "messages.json"
    write_json(
        file_path,
        {
            "steps": [
                {"title": "First"},
                {"title": "Second"},
            ],
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    parsed_lines, _translations = adapter.parse_file(str(file_path))
    for line in parsed_lines:
        if line.get("key") == "/steps/1/title":
            line["value"] = "Zweiter"

    assert json.loads(adapter.reassemble_file(parsed_lines)) == {
        "steps": [
            {"title": "First"},
            {"title": "Zweiter"},
        ],
    }


def test_json_adapter_synchronizes_source_and_target_string_keys(tmp_path):
    source_path = tmp_path / "messages.json"
    target_path = tmp_path / "messages_de.json"
    write_json(
        source_path,
        {
            "hello": "Hello",
            "nested": {
                "title": "Title",
                "non_string": 1,
            },
        },
    )
    write_json(
        target_path,
        {
            "hello": "Hallo",
            "legacy": "Remove me",
            "nested": {
                "extra": "Remove nested extra",
            },
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    missing_keys, extra_keys = adapter.synchronize_keys(str(target_path), str(source_path))

    assert missing_keys == {"/nested/title"}
    assert extra_keys == {"/legacy", "/nested/extra"}
    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "hello": "Hallo",
        "nested": {
            "title": "Title",
        },
    }


def test_json_adapter_synchronizes_array_string_keys(tmp_path):
    source_path = tmp_path / "messages.json"
    target_path = tmp_path / "messages_de.json"
    write_json(
        source_path,
        {
            "steps": [
                {"title": "First"},
                {"title": "Second"},
            ],
        },
    )
    write_json(
        target_path,
        {
            "steps": [
                {"title": "Erster"},
            ],
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    missing_keys, extra_keys = adapter.synchronize_keys(str(target_path), str(source_path))

    assert missing_keys == {"/steps/1/title"}
    assert extra_keys == set()
    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "steps": [
            {"title": "Erster"},
            {"title": "Second"},
        ],
    }


def test_json_adapter_deletes_extra_array_entries_from_highest_index(tmp_path):
    source_path = tmp_path / "messages.json"
    target_path = tmp_path / "messages_de.json"
    write_json(
        source_path,
        {
            "steps": [
                {"title": "First"},
            ],
        },
    )
    write_json(
        target_path,
        {
            "steps": [
                {"title": "Erster"},
                {"title": "Legacy second"},
                {"title": "Legacy third"},
            ],
        },
    )

    adapter = get_localization_adapter(JSON_FORMAT)
    missing_keys, extra_keys = adapter.synchronize_keys(str(target_path), str(source_path))

    assert missing_keys == set()
    assert extra_keys == {"/steps/1/title", "/steps/2/title"}
    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "steps": [
            {"title": "Erster"},
        ],
    }


def test_json_adapter_lints_invalid_json(tmp_path):
    file_path = tmp_path / "messages.json"
    file_path.write_text('{"hello": "Hello"', encoding="utf-8")

    adapter = get_localization_adapter(JSON_FORMAT)

    assert adapter.lint_file(str(file_path))
