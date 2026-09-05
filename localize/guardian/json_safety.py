"""Interpreter-independent bounds for JSON received across trust boundaries."""

from __future__ import annotations

import json
from typing import Any


MAX_JSON_NESTING_DEPTH = 64


def loads_bounded_json(
    payload: str | bytes | bytearray,
    *,
    max_depth: int = MAX_JSON_NESTING_DEPTH,
    **kwargs: Any,
) -> Any:
    """Decode JSON only after enforcing a lexical container-depth ceiling.

    Python versions do not reject deeply nested JSON at the same depth. Scan
    valid JSON's structural delimiters first so untrusted input has one stable
    limit before the recursive standard-library decoder sees it.
    """

    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
        raise ValueError("max_depth must be a positive integer.")
    if isinstance(payload, str):
        text = payload
    elif isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8")
    else:
        raise TypeError("JSON payload must be text or UTF-8 bytes.")

    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError(
                    f"JSON exceeds the maximum nesting depth of {max_depth}."
                )
        elif character in "]}" and depth:
            depth -= 1

    return json.loads(text, **kwargs)
