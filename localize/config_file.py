"""Reading of the pipeline configuration file."""

from __future__ import annotations

import os
from typing import Any, Dict

import yaml


_NO_DEFAULTS_HINT = "The run does not fall back to default settings when the config file is broken."


class ConfigLoadError(ValueError):
    """The configuration file exists but cannot be used."""


def config_file_missing(config_file_path: str | os.PathLike[str]) -> bool:
    """Return True only when nothing exists at the configured path.

    ``os.path.exists`` also answers False when a parent directory cannot be
    entered, which would hide an unreadable config behind the default settings.
    Any other error leaves the path to ``read_config_payload``, which reports it.
    """
    try:
        os.stat(config_file_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _has_no_settings(text: str) -> bool:
    """True for a document without content: empty, only comments, or a bare ``---``.

    ``yaml.safe_load`` returns None for these and for an explicit ``null`` or
    ``~``; only the composed node tells them apart.
    """
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    return node is None or (isinstance(node, yaml.ScalarNode) and node.value == "")


def read_config_payload(config_file_path: str) -> Dict[str, Any]:
    """Return the settings stored in an existing configuration file.

    A broken config must not be treated as a missing one: the default settings
    point at a placeholder repository, so the run would fail later with an
    error about the wrong thing. Callers decide what a missing file means; this
    function expects the file to exist. A file without settings (empty, only
    comments, or a bare ``---``) returns an empty mapping; an explicit ``null``
    is rejected like any other value that is not a mapping.
    """
    try:
        with open(config_file_path, "r", encoding="utf-8") as config_file:
            text = config_file.read()
        payload = yaml.safe_load(text)
        if payload is None and _has_no_settings(text):
            return {}
    except (OSError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise ConfigLoadError(
            f"Configuration file '{config_file_path}' cannot be read as YAML: {exc}. "
            f"{_NO_DEFAULTS_HINT}"
        ) from exc
    if not isinstance(payload, dict):
        found = "null" if payload is None else f"a {type(payload).__name__}"
        raise ConfigLoadError(
            f"Configuration file '{config_file_path}' must contain a YAML mapping of "
            f"settings (key: value), not {found}. {_NO_DEFAULTS_HINT}"
        )
    return payload
