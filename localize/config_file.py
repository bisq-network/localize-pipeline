"""Reading of the pipeline configuration file."""

from __future__ import annotations

from typing import Any, Dict

import yaml


_NO_DEFAULTS_HINT = "The run does not fall back to default settings when the config file is broken."


class ConfigLoadError(ValueError):
    """The configuration file exists but cannot be used."""


def read_config_payload(config_file_path: str) -> Dict[str, Any]:
    """Return the settings stored in an existing configuration file.

    A broken config must not be treated as a missing one: the default settings
    point at a placeholder repository, so the run would fail later with an
    error about the wrong thing. Callers decide what a missing file means; this
    function expects the file to exist. A file without settings (empty, or only
    comments) returns an empty mapping.
    """
    try:
        with open(config_file_path, "r", encoding="utf-8") as config_file:
            payload = yaml.safe_load(config_file)
    except (OSError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise ConfigLoadError(
            f"Configuration file '{config_file_path}' cannot be read as YAML: {exc}. "
            f"{_NO_DEFAULTS_HINT}"
        ) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ConfigLoadError(
            f"Configuration file '{config_file_path}' must contain a YAML mapping of "
            f"settings (key: value), not a {type(payload).__name__}. {_NO_DEFAULTS_HINT}"
        )
    return payload
