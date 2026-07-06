"""Small atomic file-write helpers for pipeline state and reports."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def write_text_atomic(path: str | os.PathLike[str], text: str) -> None:
    """Write text by replacing a temp file in the destination directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    sort_keys: bool = False,
) -> None:
    """Write a JSON object atomically with the repository's report formatting."""
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=sort_keys,
    ) + "\n"
    write_text_atomic(path, text)
