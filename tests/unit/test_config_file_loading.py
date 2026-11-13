"""A config file that cannot be used must stop the run, not fall back to defaults."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

from localize import cli
from localize.app_config import _load_yaml_config, load_app_config
from localize.config_file import ConfigLoadError


_BROKEN_CONFIGS = {
    "invalid-yaml": b"target_project_root: /repo\ninput_folder: [i18n\n",
    "list": b"- target_project_root\n- input_folder\n",
    "string": b"target_project_root /repo\n",
    "not-utf8": b"target_project_root: /rep\xff\n",
    "deep-nesting": b"[" * 100_000 + b"]" * 100_000,
    "null": b"null\n",
    "tilde": b"--- ~\n",
}

_needs_permission_checks = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root can read files without read permission",
)


def _use_config_file(monkeypatch, path) -> None:
    monkeypatch.setenv("TRANSLATOR_CONFIG_FILE", str(path))


def _config_in_locked_directory(tmp_path: Path) -> Path:
    """A config file that exists, inside a directory that cannot be entered.

    ``os.path.exists`` answers False for it, the same as for a missing file.
    """
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    config_path = locked_dir / "config.yaml"
    config_path.write_text("dry_run: true\n", encoding="utf-8")
    locked_dir.chmod(0)
    return config_path


def test_missing_config_file_still_uses_defaults(tmp_path, monkeypatch, capsys):
    """Local development runs without a config file."""
    _use_config_file(monkeypatch, tmp_path / "config.yaml")

    assert _load_yaml_config(str(tmp_path)) == {}
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [b"", b"# all settings are commented out\n", b"--- # settings follow later\n"],
    ids=["empty", "comments-only", "document-marker-only"],
)
def test_empty_config_file_still_uses_defaults(tmp_path, monkeypatch, capsys, content):
    """A file without settings is the same as no file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(content)
    _use_config_file(monkeypatch, config_path)

    assert _load_yaml_config(str(tmp_path)) == {}
    assert "is empty" in capsys.readouterr().err


@pytest.mark.parametrize("content", _BROKEN_CONFIGS.values(), ids=_BROKEN_CONFIGS.keys())
def test_broken_config_file_stops_the_load(tmp_path, monkeypatch, content):
    """Default settings point at a placeholder repository, not at the operator's one."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(content)
    _use_config_file(monkeypatch, config_path)

    with pytest.raises(ConfigLoadError) as error:
        _load_yaml_config(str(tmp_path))

    assert str(config_path) in str(error.value)
    assert "default settings" in str(error.value)


@pytest.mark.parametrize(
    ("kind", "found"),
    [("list", "not a list"), ("null", "not null"), ("tilde", "not null")],
)
def test_config_that_is_not_a_mapping_names_the_expected_shape(
    tmp_path, monkeypatch, kind, found
):
    """An explicit null is a value, not an empty file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(_BROKEN_CONFIGS[kind])
    _use_config_file(monkeypatch, config_path)

    with pytest.raises(ConfigLoadError, match="must contain a YAML mapping") as error:
        _load_yaml_config(str(tmp_path))

    assert found in str(error.value)


def test_config_path_that_is_a_directory_stops_the_load(tmp_path, monkeypatch):
    """Any read error stops the load, not only a parse error."""
    config_dir = tmp_path / "config.yaml"
    config_dir.mkdir()
    _use_config_file(monkeypatch, config_dir)

    with pytest.raises(ConfigLoadError, match="cannot be read"):
        _load_yaml_config(str(tmp_path))


@_needs_permission_checks
def test_config_file_without_read_permission_stops_the_load(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dry_run: true\n", encoding="utf-8")
    config_path.chmod(0)
    _use_config_file(monkeypatch, config_path)

    try:
        with pytest.raises(ConfigLoadError, match="cannot be read"):
            _load_yaml_config(str(tmp_path))
    finally:
        config_path.chmod(0o600)


@_needs_permission_checks
def test_config_file_in_a_locked_directory_is_not_treated_as_missing(tmp_path, monkeypatch):
    """Only a path that does not exist means default settings."""
    config_path = _config_in_locked_directory(tmp_path)
    _use_config_file(monkeypatch, config_path)

    try:
        with pytest.raises(ConfigLoadError, match="cannot be read") as error:
            _load_yaml_config(str(tmp_path))
    finally:
        config_path.parent.chmod(0o700)

    assert str(config_path) in str(error.value)


def test_broken_config_stops_load_app_config_before_a_model_provider_exists(
    tmp_path, monkeypatch
):
    """The run fails before it can spend a model request."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(_BROKEN_CONFIGS["invalid-yaml"])
    _use_config_file(monkeypatch, config_path)

    with patch("localize.app_config.setup_logger", return_value=MagicMock()), patch(
        "localize.app_config.create_model_provider"
    ) as create_model_provider:
        with pytest.raises(ConfigLoadError):
            load_app_config()

    create_model_provider.assert_not_called()


def _valid_preflight_config(tmp_path: Path) -> dict:
    i18n = tmp_path / "repo" / "i18n"
    i18n.mkdir(parents=True)
    return {
        "target_project_root": str(i18n.parent),
        "input_folder": str(i18n),
        "localization_format": "json",
        "localization_layout": "suffix",
        "dry_run": True,
        "supported_locales": [{"code": "de", "name": "German"}],
    }


@pytest.mark.parametrize("kind", ["invalid-yaml", "list", "not-utf8", "deep-nesting", "null"])
@pytest.mark.parametrize("command", ["check", "validate", "doctor", "smoke"])
def test_cli_preflight_fails_on_a_broken_config(tmp_path, capsys, command, kind):
    """Every preflight command rejects what the run rejects."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(_BROKEN_CONFIGS[kind])

    exit_code = cli.main([command, "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert str(config_path) in captured.err
    assert "OK" not in captured.out

    config_path.write_text(yaml.safe_dump(_valid_preflight_config(tmp_path)), encoding="utf-8")
    assert cli.main([command, "--config", str(config_path)]) == 0


@_needs_permission_checks
@pytest.mark.parametrize("command", ["check", "validate", "doctor", "smoke"])
def test_cli_preflight_reports_a_config_in_a_locked_directory(tmp_path, capsys, command):
    """The file is there but cannot be read: one error line, not a traceback."""
    config_path = _config_in_locked_directory(tmp_path)

    try:
        exit_code = cli.main([command, "--config", str(config_path)])
    finally:
        config_path.parent.chmod(0o700)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert str(config_path) in captured.err
    assert "not found" not in captured.err


def test_cli_run_reports_a_config_load_error_without_a_traceback(tmp_path, capsys):
    """`localize run` loads the config when it imports the runtime module."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dry_run: true\n", encoding="utf-8")
    message = f"Configuration file '{config_path}' cannot be read as YAML: boom."

    try:
        with patch(
            "localize.cli.importlib.import_module",
            side_effect=ConfigLoadError(message),
        ):
            exit_code = cli.main(["run", "--config", str(config_path), "--dry-run"])
    finally:
        os.environ.pop("TRANSLATOR_CONFIG_FILE", None)
        os.environ.pop("LOCALIZE_DRY_RUN", None)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err.strip() == f"error: {message}"


def _dry_run_in_a_fresh_process(config_path: Path, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("TRANSLATOR_CONFIG_FILE", None)
    env.pop("LOCALIZE_DRY_RUN", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return subprocess.run(
        [sys.executable, "-m", "localize.cli", "run", "--config", str(config_path), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )


def test_dry_run_with_a_broken_config_stops_in_a_fresh_process(tmp_path):
    """End to end: the real import-time load, as `update-translations.sh` runs it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(_BROKEN_CONFIGS["invalid-yaml"])

    result = _dry_run_in_a_fresh_process(config_path, tmp_path)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert f"error: Configuration file '{config_path}' cannot be read as YAML" in result.stderr


@_needs_permission_checks
def test_dry_run_with_a_config_in_a_locked_directory_stops_in_a_fresh_process(tmp_path):
    config_path = _config_in_locked_directory(tmp_path)

    try:
        result = _dry_run_in_a_fresh_process(config_path, tmp_path)
    finally:
        config_path.parent.chmod(0o700)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert f"error: Configuration file '{config_path}' cannot be read as YAML" in result.stderr
