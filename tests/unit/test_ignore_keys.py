"""Tests for configured translation-key ignore patterns."""

import re

import pytest

from localize.ignore_keys import compile_ignore_key_patterns, is_ignored_key


def test_json_pointer_comment_keys_match_ignore_pattern():
    patterns = compile_ignore_key_patterns([r"^/#\d+$"])

    assert is_ignored_key("/#1", patterns)
    assert is_ignored_key("/#52", patterns)
    assert not is_ignored_key("/#abc", patterns)
    assert not is_ignored_key("/nav/settings", patterns)


def test_nested_json_pointer_keys_can_be_ignored():
    patterns = compile_ignore_key_patterns([r"^/metadata/comments/"])

    assert is_ignored_key("/metadata/comments/0", patterns)
    assert is_ignored_key("/metadata/comments/source", patterns)
    assert not is_ignored_key("/metadata/title", patterns)


def test_properties_keys_use_the_adapter_key_verbatim():
    patterns = compile_ignore_key_patterns([r"^debug\.", r"\.comment$"])

    assert is_ignored_key("debug.section", patterns)
    assert is_ignored_key("screen.comment", patterns)
    assert not is_ignored_key("screen.title", patterns)


def test_empty_patterns_are_noop():
    assert compile_ignore_key_patterns([]) == []
    assert not is_ignored_key("/#1", [])


def test_compiled_patterns_are_passed_through():
    compiled_pattern = re.compile(r"^/#\d+$")

    patterns = compile_ignore_key_patterns([compiled_pattern, r"^/metadata$"])

    assert patterns[0] is compiled_pattern
    assert is_ignored_key("/#1", patterns)
    assert is_ignored_key("/metadata", patterns)
    assert not is_ignored_key("/title", patterns)


def test_invalid_pattern_reports_the_pattern():
    with pytest.raises(ValueError, match=r"Invalid ignore_key_patterns regex '\['"):
        compile_ignore_key_patterns(["["])
