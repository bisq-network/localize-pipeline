"""Tests for stable JSON nesting limits across supported Python versions."""

from __future__ import annotations

import json

import pytest

from localize.guardian.json_safety import (
    MAX_JSON_NESTING_DEPTH,
    loads_bounded_json,
)


def test_bounded_json_accepts_the_exact_container_depth_limit() -> None:
    payload = "[" * MAX_JSON_NESTING_DEPTH + "0" + "]" * MAX_JSON_NESTING_DEPTH

    decoded = loads_bounded_json(payload)
    for _ in range(MAX_JSON_NESTING_DEPTH):
        assert isinstance(decoded, list)
        decoded = decoded[0]
    assert decoded == 0


@pytest.mark.parametrize("payload_type", [str, bytes, bytearray])
def test_bounded_json_rejects_excessive_nesting_before_decode(payload_type) -> None:
    text = "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (
        MAX_JSON_NESTING_DEPTH + 1
    )
    payload = text if payload_type is str else payload_type(text.encode("utf-8"))

    with pytest.raises(ValueError, match="maximum nesting depth"):
        loads_bounded_json(payload)


def test_bounded_json_ignores_escaped_delimiters_inside_strings() -> None:
    value = "[" * 1000 + '\\"' + "}" * 1000
    payload = json.dumps({"value": value})

    assert loads_bounded_json(payload) == {"value": value}


def test_bounded_json_rejects_non_utf8_byte_encodings_before_scanning() -> None:
    text = "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (
        MAX_JSON_NESTING_DEPTH + 1
    )

    with pytest.raises(UnicodeDecodeError):
        loads_bounded_json(text.encode("utf-16"))


def test_bounded_json_preserves_decoder_validation() -> None:
    with pytest.raises(json.JSONDecodeError):
        loads_bounded_json('{"broken":]')
