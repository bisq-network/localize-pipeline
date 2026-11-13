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
}


def _use_config_file(monkeypatch, path) -> None:
    monkeypatch.setenv("TRANSLATOR_CONFIG_FILE", str(path))


def test_missing_config_file_still_uses_defaults(tmp_path, monkeypatch, capsys):
    """Local development runs without a config file."""
    _use_config_file(monkeypatch, tmp_path / "config.yaml")

    assert _load_yaml_config(str(tmp_path)) == {}
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [b"", b"# all settings are commented out\n"],
    ids=["empty", "comments-only"],
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


def test_config_that_is_not_a_mapping_names_the_expected_shape(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(_BROKEN_CONFIGS["list"])
    _use_config_file(monkeypatch, config_path)

    with pytest.raises(ConfigLoadError, match="must contain a YAML mapping"):
        _load_yaml_config(str(tmp_path))


def test_config_path_that_is_a_directory_stops_the_load(tmp_path, monkeypatch):
    """Any read error stops the load, not only a parse error."""
    config_dir = tmp_path / "config.yaml"
    config_dir.mkdir()
    _use_config_file(monkeypatch, config_dir)

    with pytest.raises(ConfigLoadError, match="cannot be read"):
        _load_yaml_config(str(tmp_path))


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root can read files without read permission",
)
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


@pytest.mark.parametrize("kind", ["invalid-yaml", "list", "not-utf8", "deep-nesting"])
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


def test_dry_run_with_a_broken_config_stops_in_a_fresh_process(tmp_path):
    """End to end: the real import-time load, as `update-translations.sh` runs it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(_BROKEN_CONFIGS["invalid-yaml"])
    env = os.environ.copy()
    env.pop("TRANSLATOR_CONFIG_FILE", None)
    env.pop("LOCALIZE_DRY_RUN", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

    result = subprocess.run(
        [sys.executable, "-m", "localize.cli", "run", "--config", str(config_path), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert f"error: Configuration file '{config_path}' cannot be read as YAML" in result.stderr
