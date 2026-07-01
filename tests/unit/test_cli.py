import os
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from localize import cli
from localize.formats import unregister_localization_adapter
from localize.translation_memory import TranslationMemory, load_translation_memory, save_translation_memory


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


def test_cli_doctor_prints_redacted_effective_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": "i18n",
            "translation_queue_folder": "translation_queue",
            "translated_queue_folder": "translated_queue",
            "model_provider": "aisuite",
            "model_name": "gpt-4o-mini",
            "review_model_name": "gpt-4o",
            "semantic_review": {"enabled": True, "model": "gpt-4o"},
            "dry_run": False,
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Localize Pipeline doctor" in captured.out
    assert "profile: default" in captured.out
    assert f"target_project_root: {repo}" in captured.out
    assert f"input_folder: {i18n}" in captured.out
    assert "model_provider: aisuite" in captured.out
    assert "semantic_review: enabled" in captured.out
    assert "OPENAI_API_KEY: set" in captured.out
    assert "sk-secret-value" not in captured.out


def test_cli_smoke_is_read_only_and_creates_missing_runtime_queue_dirs(tmp_path, capsys):
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    queue = tmp_path / "scratch" / "translation_queue"
    translated = tmp_path / "scratch" / "translated_queue"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": "i18n",
            "translation_queue_folder": str(queue),
            "translated_queue_folder": str(translated),
            "dry_run": True,
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )

    exit_code = cli.main(["smoke", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Smoke OK" in captured.out
    assert queue.is_dir()
    assert translated.is_dir()


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
    monkeypatch.delenv("TRANSLATOR_CONFIG_FILE", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dry_run: true\n", encoding="utf-8")
    runtime = SimpleNamespace(main=AsyncMock())

    try:
        with patch("localize.cli.importlib.import_module", return_value=runtime) as import_module:
            exit_code = cli.main(["run", "--config", str(config_path)])

        assert exit_code == 0
        assert os.environ["TRANSLATOR_CONFIG_FILE"] == str(config_path)
        import_module.assert_called_once_with("localize.translate_localization_files")
        runtime.main.assert_awaited_once()
    finally:
        os.environ.pop("TRANSLATOR_CONFIG_FILE", None)


def test_cli_run_dry_run_sets_runtime_override(tmp_path, monkeypatch):
    monkeypatch.delenv("TRANSLATOR_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LOCALIZE_DRY_RUN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dry_run: false\n", encoding="utf-8")
    runtime = SimpleNamespace(main=AsyncMock())

    try:
        with patch("localize.cli.importlib.import_module", return_value=runtime):
            exit_code = cli.main(["run", "--config", str(config_path), "--dry-run"])

        assert exit_code == 0
        assert os.environ["TRANSLATOR_CONFIG_FILE"] == str(config_path)
        assert os.environ["LOCALIZE_DRY_RUN"] == "true"
        runtime.main.assert_awaited_once()
    finally:
        os.environ.pop("TRANSLATOR_CONFIG_FILE", None)
        os.environ.pop("LOCALIZE_DRY_RUN", None)


def test_cli_init_delegates_to_existing_init_config():
    with patch("localize.cli.init_config_main", return_value=0) as init_main:
        exit_code = cli.main(["init", "--input-folder", "i18n"])

    assert exit_code == 0
    init_main.assert_called_once_with(["--input-folder", "i18n"])


def test_cli_bootstrap_pr_delegates_to_onboarding_generator(capsys):
    result = SimpleNamespace(
        branch_name="localize/onboarding",
        commit_sha="abc123",
        pushed=False,
        opened_pr=False,
    )
    with patch("localize.cli.create_bootstrap_pr", return_value=result) as create:
        exit_code = cli.main([
            "bootstrap-pr",
            "--target-project-root",
            "/repo",
            "--input-folder",
            "i18n",
            "--action-ref",
            "v0.1.3",
            "--plugin-module",
            "target_repo.localize_adapter",
            "--plugin-install-command",
            "python -m pip install .",
        ])

    captured = capsys.readouterr()
    assert exit_code == 0
    options = create.call_args.args[0]
    assert options.target_project_root == "/repo"
    assert options.input_folder == "i18n"
    assert options.action_ref == "v0.1.3"
    assert options.plugin_modules == ("target_repo.localize_adapter",)
    assert options.plugin_install_command == "python -m pip install ."
    assert "Created onboarding commit abc123" in captured.out


def test_cli_quality_gate_delegates_to_rerunnable_reporter(tmp_path):
    with patch("localize.cli.translation_quality_gate_main", return_value=0) as quality_main:
        exit_code = cli.main([
            "quality-gate",
            "--repo-root",
            "/repo",
            "--input-folder",
            "/repo/i18n",
            "--config",
            "/repo/config.yaml",
            "--validation-summary",
            "/repo/logs/summary.json",
            "--output-json",
            "/tmp/report.json",
            "--output-markdown",
            "/tmp/report.md",
            "--changed-files",
            "i18n/messages_de.properties",
        ])

    assert exit_code == 0
    quality_main.assert_called_once_with([
        "--repo-root",
        "/repo",
        "--input-folder",
        "/repo/i18n",
        "--config",
        "/repo/config.yaml",
        "--validation-summary",
        "/repo/logs/summary.json",
        "--output-json",
        "/tmp/report.json",
        "--output-markdown",
        "/tmp/report.md",
        "--changed-files",
        "i18n/messages_de.properties",
    ])


def test_cli_memory_import_export_stats_and_suggest(tmp_path, capsys):
    source = tmp_path / "source-memory.json"
    destination = tmp_path / "destination-memory.json"
    exported = tmp_path / "exported-memory.json"

    memory = TranslationMemory()
    memory.record("Save changes", "Änderungen speichern", locale="de", format_id="json")
    save_translation_memory(source, memory)

    assert cli.main(["memory", "export", "--memory-file", str(source), "--output", str(exported)]) == 0
    assert exported.exists()

    assert cli.main(["memory", "import", "--memory-file", str(destination), "--input", str(exported)]) == 0
    imported = load_translation_memory(destination)
    assert imported.lookup("Save changes", locale="de", format_id="json") == "Änderungen speichern"

    assert cli.main(["memory", "stats", "--memory-file", str(destination)]) == 0
    stats_output = capsys.readouterr().out
    assert "active_entries: 1" in stats_output
    assert "locales: de" in stats_output

    assert cli.main([
        "memory",
        "suggest",
        "--memory-file",
        str(destination),
        "--source-text",
        "Save change",
        "--locale",
        "de",
        "--format-id",
        "json",
        "--min-score",
        "0.75",
    ]) == 0
    suggestions = capsys.readouterr().out
    assert "Save changes" in suggestions
    assert "Änderungen speichern" in suggestions


def test_cli_memory_import_rejects_invalid_input_file(tmp_path, capsys):
    destination = tmp_path / "destination-memory.json"
    invalid = tmp_path / "invalid-memory.json"
    invalid.write_text("{not-json", encoding="utf-8")

    exit_code = cli.main(["memory", "import", "--memory-file", str(destination), "--input", str(invalid)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "invalid translation memory" in captured.err
    assert not destination.exists()


def test_cli_memory_promote_rejects_corrupt_target_file(tmp_path, capsys):
    corrupt = tmp_path / "corrupt-memory.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    exit_code = cli.main([
        "memory",
        "promote",
        "--memory-file",
        str(corrupt),
        "--source-text",
        "Save",
        "--target-text",
        "Speichern",
        "--locale",
        "de",
        "--format-id",
        "json",
    ])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "invalid translation memory" in captured.err
    assert corrupt.read_text(encoding="utf-8") == "{not-json"


def test_cli_memory_suggest_rejects_negative_limit(capsys):
    try:
        cli.main([
            "memory",
            "suggest",
            "--source-text",
            "Save",
            "--locale",
            "de",
            "--format-id",
            "json",
            "--limit",
            "-1",
        ])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("negative limit should fail argument parsing")

    assert "non-negative integer" in capsys.readouterr().err


def test_pyproject_exposes_localize_console_script():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "localize-pipeline"
    assert pyproject["project"]["version"] == "0.1.3"
    assert pyproject["project"]["urls"]["Repository"] == "https://github.com/bisq-network/localize-pipeline"
    assert pyproject["project"]["urls"]["Changelog"]
    assert pyproject["project"]["urls"]["Issues"] == "https://github.com/bisq-network/localize-pipeline/issues"
    assert "Programming Language :: Python :: 3 :: Only" in pyproject["project"]["classifiers"]
    assert pyproject["project"]["scripts"]["localize"] == "localize.cli:main"
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["include"] == ["localize*"]


def test_readme_documents_cli_quickstart():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "localize init" in readme
    assert "localize check" in readme
    assert "localize validate" in readme
    assert "localize run --dry-run" in readme
    assert "./init.sh" not in readme


def test_package_version_matches_pyproject():
    import localize

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert localize.__version__ == pyproject["project"]["version"]


def test_generic_examples_are_cli_validatable():
    assert cli.main(["validate", "--config", str(REPO_ROOT / "examples/generic-json/config.yaml")]) == 0
    assert cli.main([
        "validate",
        "--config",
        str(REPO_ROOT / "examples/generic-java-properties/config.yaml"),
    ]) == 0
