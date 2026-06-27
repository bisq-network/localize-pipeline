import os
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from src import cli


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cli_formats_lists_registered_formats(capsys):
    exit_code = cli.main(["formats"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "java_properties" in captured.out
    assert "json" in captured.out


def test_cli_validate_reports_valid_config(tmp_path, capsys):
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": str(i18n),
            "localization_format": "json",
            "localization_layout": "suffix",
            "dry_run": True,
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )

    exit_code = cli.main(["validate", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Configuration OK" in captured.out


def test_cli_validate_accepts_mixed_format_profiles(tmp_path, capsys):
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": str(i18n),
            "localization_formats": [
                {"id": "java_properties", "layout": "suffix"},
                {"id": "json", "layout": {"id": "locale_directory", "source_locale": "en"}},
            ],
            "dry_run": True,
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )

    exit_code = cli.main(["validate", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Configuration OK" in captured.out


def test_cli_validate_returns_nonzero_for_config_errors(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("target_project_root: /missing\ninput_folder: /missing/i18n\n", encoding="utf-8")

    exit_code = cli.main(["validate", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err


def test_cli_run_sets_config_env_and_delegates_to_runtime(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dry_run: true\n", encoding="utf-8")
    runtime = SimpleNamespace(main=AsyncMock())

    with patch("src.cli.importlib.import_module", return_value=runtime) as import_module:
        exit_code = cli.main(["run", "--config", str(config_path)])

    assert exit_code == 0
    assert os.environ["TRANSLATOR_CONFIG_FILE"] == str(config_path)
    import_module.assert_called_once_with("src.translate_localization_files")
    runtime.main.assert_awaited_once()


def test_cli_init_delegates_to_existing_init_config():
    with patch("src.cli.init_config_main", return_value=0) as init_main:
        exit_code = cli.main(["init", "--input-folder", "i18n"])

    assert exit_code == 0
    init_main.assert_called_once_with(["--input-folder", "i18n"])


def test_pyproject_exposes_localize_console_script():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["localize"] == "src.cli:main"


def test_readme_documents_cli_quickstart():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "localize init" in readme
    assert "localize validate" in readme
    assert "localize run" in readme


def test_generic_examples_are_cli_validatable():
    assert cli.main(["validate", "--config", str(REPO_ROOT / "examples/generic-json/config.yaml")]) == 0
    assert cli.main([
        "validate",
        "--config",
        str(REPO_ROOT / "examples/generic-java-properties/config.yaml"),
    ]) == 0
