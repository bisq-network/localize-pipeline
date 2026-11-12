"""Reading of the project glossary file."""

from __future__ import annotations

import json
from typing import Any, Dict


class GlossaryLoadError(RuntimeError):
    """The glossary file exists but cannot be used."""


def read_glossary_payload(glossary_file_path: str) -> Dict[str, Any]:
    """Return the JSON object stored in an existing glossary file.

    A glossary that cannot be parsed must not be treated as an empty one: the
    run would pay for translations that ignore the required terms. Callers
    decide what a missing file means; this function expects the file to exist.
    """
    try:
        with open(glossary_file_path, "r", encoding="utf-8") as glossary_file:
            payload = json.load(glossary_file)
    except (OSError, ValueError) as exc:
        raise GlossaryLoadError(
            f"Glossary file '{glossary_file_path}' cannot be read as JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise GlossaryLoadError(
            f"Glossary file '{glossary_file_path}' must contain a JSON object."
        )
    return payload
