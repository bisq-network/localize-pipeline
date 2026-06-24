"""Unit tests for the Docker-free init/quickstart helper."""
import os

import pytest
import yaml

from src.init_config import (
    build_config,
    code_to_name,
    detect_locales,
    render_config,
    write_config,
)


def _make_props(folder, names):
    for name in names:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write("key=value\n")


class TestDetectLocales:
    def test_detects_locale_suffixes_excluding_source(self, tmp_path):
        _make_props(str(tmp_path), [
            "app_en.properties", "app_de.properties", "app_es.properties",
        ])
        locales = detect_locales(str(tmp_path), source_locale="en")
        codes = [loc["code"] for loc in locales]
        assert codes == ["de", "es"]  # sorted, source excluded

    def test_detects_region_codes(self, tmp_path):
        _make_props(str(tmp_path), ["app_en.properties", "app_pt_BR.properties"])
        codes = [loc["code"] for loc in detect_locales(str(tmp_path), source_locale="en")]
        assert "pt_BR" in codes

    def test_ignores_non_locale_and_non_properties_files(self, tmp_path):
        _make_props(str(tmp_path), ["app_de.properties", "README.properties"])
        with open(os.path.join(str(tmp_path), "notes.txt"), "w") as f:
            f.write("x")
        codes = [loc["code"] for loc in detect_locales(str(tmp_path), source_locale="en")]
        assert codes == ["de"]

    def test_each_locale_has_a_name(self, tmp_path):
        _make_props(str(tmp_path), ["app_en.properties", "app_de.properties"])
        locales = detect_locales(str(tmp_path), source_locale="en")
        assert locales[0]["name"] == "German"

    def test_missing_folder_returns_empty(self, tmp_path):
        assert detect_locales(str(tmp_path / "nope"), source_locale="en") == []

    def test_detects_locales_in_nested_subdirectories(self, tmp_path):
        # The runtime pipeline walks the tree recursively; detection must match.
        nested = tmp_path / "resources" / "mobile"
        nested.mkdir(parents=True)
        _make_props(str(nested), ["app_en.properties", "app_fr.properties"])
        _make_props(str(tmp_path), ["top_de.properties"])
        codes = [loc["code"] for loc in detect_locales(str(tmp_path), source_locale="en")]
        assert codes == ["de", "fr"]

    def test_deduplicates_locale_across_multiple_base_files(self, tmp_path):
        _make_props(str(tmp_path), [
            "a_de.properties", "b_de.properties", "a_en.properties",
        ])
        codes = [loc["code"] for loc in detect_locales(str(tmp_path), source_locale="en")]
        assert codes == ["de"]


class TestCodeToName:
    def test_known_code(self):
        assert code_to_name("fr") == "French"

    def test_region_code_known(self):
        assert code_to_name("pt_BR") == "Brazilian Portuguese"

    def test_unknown_code_falls_back_to_code(self):
        assert code_to_name("xx") == "xx"


class TestBuildConfig:
    def test_defaults_to_git_source(self):
        cfg = build_config(
            target_project_root="/repo", input_folder="i18n",
            locales=[{"code": "de", "name": "German"}],
        )
        assert cfg["translation_source"] == "git"
        assert cfg["target_project_root"] == "/repo"
        assert cfg["input_folder"] == "i18n"
        assert cfg["supported_locales"] == [{"code": "de", "name": "German"}]
        assert "model_name" in cfg and "review_model_name" in cfg

    def test_optional_base_url_included_when_provided(self):
        cfg = build_config(
            target_project_root="/repo", input_folder="i18n", locales=[],
            api_base_url="http://localhost:11434/v1",
        )
        assert cfg["api_base_url"] == "http://localhost:11434/v1"

    def test_no_base_url_key_when_not_provided(self):
        cfg = build_config(target_project_root="/repo", input_folder="i18n", locales=[])
        assert "api_base_url" not in cfg


class TestRenderConfig:
    def test_renders_valid_roundtrippable_yaml(self):
        cfg = build_config(
            target_project_root="/repo", input_folder="i18n",
            locales=[{"code": "de", "name": "German"}],
        )
        text = render_config(cfg)
        loaded = yaml.safe_load(text)
        assert loaded["target_project_root"] == "/repo"
        assert loaded["supported_locales"][0]["code"] == "de"
        assert loaded["translation_source"] == "git"

    def test_includes_header_comment(self):
        text = render_config(build_config(
            target_project_root="/repo", input_folder="i18n", locales=[]))
        assert text.lstrip().startswith("#")


class TestWriteConfig:
    def test_writes_new_file(self, tmp_path):
        target = str(tmp_path / "config.yaml")
        write_config(target, "model_name: gpt-4o-mini\n")
        assert os.path.exists(target)

    def test_refuses_to_overwrite_without_flag(self, tmp_path):
        target = str(tmp_path / "config.yaml")
        write_config(target, "a: 1\n")
        with pytest.raises(FileExistsError):
            write_config(target, "a: 2\n")

    def test_overwrites_with_flag(self, tmp_path):
        target = str(tmp_path / "config.yaml")
        write_config(target, "a: 1\n")
        write_config(target, "a: 2\n", overwrite=True)
        with open(target, encoding="utf-8") as f:
            assert "a: 2" in f.read()


class TestMainErrorHandling:
    def test_main_handles_write_oserror_without_traceback(self, tmp_path, monkeypatch, capsys):
        """A write failure (e.g. PermissionError) becomes a clean error, not a traceback."""
        import src.init_config as init_config

        folder = tmp_path / "i18n"
        folder.mkdir()
        (folder / "app_en.properties").write_text("k=v\n")
        (folder / "app_de.properties").write_text("k=v\n")

        def boom(*_args, **_kwargs):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(init_config, "write_config", boom)
        rc = init_config.main([
            "--input-folder", str(folder),
            "--output", str(tmp_path / "out.yaml"),
        ])
        assert rc == 1
        assert "Error" in capsys.readouterr().err
