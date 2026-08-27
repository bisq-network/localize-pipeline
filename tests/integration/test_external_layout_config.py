from pathlib import Path

from localize.app_config import load_app_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYOUT_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "external_layout"
LAYOUT_CONFIG = LAYOUT_ROOT / "translations" / "config.yaml"


def test_subdirectory_config_resolves_existing_external_layout(monkeypatch):
    """A config in a subdirectory must resolve every relative path to a real location.

    ``input_folder`` resolves against ``target_project_root`` while
    ``glossary_file_path`` resolves against the config file's own directory. A
    config that leaves ``target_project_root`` as ``"."`` resolves both under
    ``translations/`` instead, yielding paths that do not exist.
    """
    monkeypatch.setenv("TRANSLATOR_CONFIG_FILE", str(LAYOUT_CONFIG))
    config = load_app_config()

    assert Path(config.target_project_root) == LAYOUT_ROOT
    assert Path(config.target_project_root).is_dir()

    assert Path(config.input_folder) == LAYOUT_ROOT / "app" / "src" / "main" / "resources" / "l10n"
    assert Path(config.input_folder).is_dir()

    assert Path(config.glossary_file_path) == LAYOUT_ROOT / "translations" / "glossary.json"
    assert Path(config.glossary_file_path).is_file()
    assert config.localization_layout.base_name == "Messages"
    assert config.localization_layout.is_source_file(
        "Messages_en.properties",
        ["de"],
        config.localization_format,
    )
    assert config.localization_layout.source_path_for_target(
        "Messages_de.properties",
        ["de"],
        config.localization_format,
    ) == "Messages_en.properties"
