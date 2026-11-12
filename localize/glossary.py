"""Reading of the project glossary file."""

from __future__ import annotations

import json
from typing import Any, Dict


_EMPTY_GLOSSARY_HINT = "A glossary without terms is the empty JSON object {}."


class GlossaryLoadError(RuntimeError):
    """The glossary file exists but cannot be used."""


def read_glossary_payload(glossary_file_path: str) -> Dict[str, Any]:
    """Return the JSON object stored in an existing glossary file.

    A glossary that cannot be parsed must not be treated as an empty one: the
    run would pay for translations that ignore the required terms. Callers
    decide what a missing file means; this function expects the file to exist.
    An empty file is rejected too: it may be a glossary that was truncated. A
    project without required terms uses the empty JSON object ``{}``.
    """
    try:
        with open(glossary_file_path, "r", encoding="utf-8") as glossary_file:
            payload = json.load(glossary_file)
    except (OSError, ValueError, RecursionError) as exc:
        raise GlossaryLoadError(
            f"Glossary file '{glossary_file_path}' cannot be read as JSON: {exc}. "
            f"{_EMPTY_GLOSSARY_HINT}"
        ) from exc
    if not isinstance(payload, dict):
        raise GlossaryLoadError(
            f"Glossary file '{glossary_file_path}' must contain a JSON object. "
            f"{_EMPTY_GLOSSARY_HINT}"
        )
    return payload
