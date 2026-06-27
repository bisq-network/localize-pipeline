import os
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from localize import cli
from localize.formats import unregister_localization_adapter


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cli_formats_lists_registered_formats(capsys):
    exit_code = cli.main(["formats"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "java_properties" in captured.out
    assert "json" in captured.out


def test_cli_loads_plugin_module_before_listing_formats(tmp_path, monkeypatch, capsys):
    plugin_path = tmp_path / "demo_plugin.py"
    plugin_path.write_text(
        """
from localize.formats import LocalizationFileAdapter, LocalizationFormat, register_localization_adapter

fmt = LocalizationFormat(
    id="demo_plugin_format",
    display_name="Demo Plugin",
    file_extension=".demo",
    code_fence="text",
    locale_suffix_regex=r"_(?P<locale>[A-Za-z]{2})",
)

adapter = LocalizationFileAdapter(
    localization_format=fmt,
    parse_file=lambda path: ([], {}),
    reassemble_file=lambda lines: "",
    synchronize_keys=lambda target, source: (set(), set()),
    lint_file=lambda path: [],
    extract_changed_key_from_diff_line=lambda line: None,
    build_review_content=lambda translations, keys: "",
    escape_translation=lambda source, value: value,
)

register_localization_adapter(adapter)
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        exit_code = cli.main(["--plugin", "demo_plugin", "formats"])
        captured = capsys.readouterr()
    finally:
        unregister_localization_adapter("demo_plugin_format")

    assert exit_code == 0
    assert "demo_plugin_format" in captured.out


def test_cli_loads_plugin_module_after_subcommand(tmp_path, monkeypatch, capsys):
    plugin_path = tmp_path / "demo_plugin_late.py"
    plugin_path.write_text(
        """
from localize.formats import LocalizationFileAdapter, LocalizationFormat, register_localization_adapter

fmt = LocalizationFormat(
    id="demo_plugin_format",
    display_name="Demo Plugin",
    file_extension=".demo",
    code_fence="text",
    locale_suffix_regex=r"_(?P<locale>[A-Za-z]{2})",
)

adapter = LocalizationFileAdapter(
    localization_format=fmt,
    parse_file=lambda path: ([], {}),
    reassemble_file=lambda lines: "",
    synchronize_keys=lambda target, source: (set(), set()),
    lint_file=lambda path: [],
    extract_changed_key_from_diff_line=lambda line: None,
    build_review_content=lambda translations, keys: "",
    escape_translation=lambda source, value: value,
)

register_localization_adapter(adapter)
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    try:
        exit_code = cli.main(["formats", "--plugin", "demo_plugin_late"])
        captured = capsys.readouterr()
    finally:
        unregister_localization_adapter("demo_plugin_format")

    assert exit_code == 0
    assert "demo_plugin_format" in captured.out


def test_cli_init_loads_plugin_after_subcommand_without_forwarding_it():
    with (
        patch("localize.cli.load_plugins") as load_plugins,
        patch("localize.cli.init_config_main", return_value=0) as init_main,
    ):
        exit_code = cli.main(["init", "--plugin=demo_plugin", "--input-folder", "i18n"])

    assert exit_code == 0
    load_plugins.assert_called_once_with(["demo_plugin"])
    init_main.assert_called_once_with(["--input-folder", "i18n"])


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


def test_cli_check_alias_runs_preflight_validation(tmp_path, capsys):
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

    exit_code = cli.main(["check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Preflight OK" in captured.out


def test_cli_check_false_dry_run_env_does_not_override_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LOCALIZE_DRY_RUN", "false")
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": str(i18n),
            "dry_run": True,
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )

    exit_code = cli.main(["check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Preflight OK" in captured.out


def test_cli_check_applies_review_model_env_override(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("REVIEW_MODEL_NAME", "gpt-4o")
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": str(i18n),
            "dry_run": False,
            "model_name": "anthropic:claude-3-5-sonnet-latest",
            "review_model_name": "anthropic:claude-3-5-sonnet-latest",
            "aisuite": {"provider_configs": {"anthropic": {"api_key": "secret"}}},
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )

    exit_code = cli.main(["check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "OPENAI_API_KEY" in captured.err


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

    with patch("localize.cli.importlib.import_module", return_value=runtime) as import_module:
        exit_code = cli.main(["run", "--config", str(config_path)])

    assert exit_code == 0
    assert os.environ["TRANSLATOR_CONFIG_FILE"] == str(config_path)
    import_module.assert_called_once_with("localize.translate_localization_files")
    runtime.main.assert_awaited_once()


def test_cli_run_dry_run_sets_runtime_override(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALIZE_DRY_RUN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dry_run: false\n", encoding="utf-8")
    runtime = SimpleNamespace(main=AsyncMock())

    with patch("localize.cli.importlib.import_module", return_value=runtime):
        exit_code = cli.main(["run", "--config", str(config_path), "--dry-run"])

    assert exit_code == 0
    assert os.environ["TRANSLATOR_CONFIG_FILE"] == str(config_path)
    assert os.environ["LOCALIZE_DRY_RUN"] == "true"
    runtime.main.assert_awaited_once()


def test_cli_init_delegates_to_existing_init_config():
    with patch("localize.cli.init_config_main", return_value=0) as init_main:
        exit_code = cli.main(["init", "--input-folder", "i18n"])

    assert exit_code == 0
    init_main.assert_called_once_with(["--input-folder", "i18n"])


def test_pyproject_exposes_localize_console_script():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "localize-pipeline"
    assert pyproject["project"]["scripts"]["localize"] == "localize.cli:main"
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["include"] == ["localize*"]


def test_readme_documents_cli_quickstart():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "localize init" in readme
    assert "localize check" in readme
    assert "localize validate" in readme
    assert "localize run --dry-run" in readme


def test_generic_examples_are_cli_validatable():
    assert cli.main(["validate", "--config", str(REPO_ROOT / "examples/generic-json/config.yaml")]) == 0
    assert cli.main([
        "validate",
        "--config",
        str(REPO_ROOT / "examples/generic-java-properties/config.yaml"),
    ]) == 0
