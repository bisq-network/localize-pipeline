"""Unit tests for the Docker-free init/quickstart helper."""
import os

import pytest
import yaml

from localize.init_config import (
    build_config,
    code_to_name,
    detect_locales,
    detect_locales_for_profiles,
    parse_localization_profile_spec,
    render_config,
    write_config,
)


def _make_props(folder, names):
    for name in names:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write("key=value\n")


def _make_json(folder, names):
    for name in names:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write('{"key":"value"}\n')


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

    def test_detects_json_locale_suffixes_when_requested(self, tmp_path):
        _make_json(str(tmp_path), [
            "app.json", "app_en.json", "app_de.json", "messages.pt-BR.json", "messages.es-419.json",
        ])
        codes = [
            loc["code"]
            for loc in detect_locales(str(tmp_path), source_locale="en", localization_format="json")
        ]
        assert codes == ["de", "es-419", "pt-BR"]

    def test_detects_json_locale_directory_layout(self, tmp_path):
        for rel_path in ["en/common.json", "de/common.json", "es-419/common.json", "fr/common.json"]:
            path = tmp_path / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"key":"value"}\n', encoding="utf-8")

        codes = [
            loc["code"]
            for loc in detect_locales(
                str(tmp_path),
                source_locale="en",
                localization_format="json",
                localization_layout={"id": "locale_directory", "source_locale": "en"},
            )
        ]

        assert codes == ["de", "es-419", "fr"]

    def test_detects_json_locale_filename_layout_with_numeric_region(self, tmp_path):
        _make_json(str(tmp_path), ["en.json", "es-419.json"])

        codes = [
            loc["code"]
            for loc in detect_locales(
                str(tmp_path),
                source_locale="en",
                localization_format="json",
                localization_layout={"id": "locale_filename", "source_locale": "en"},
            )
        ]

        assert codes == ["es-419"]

    def test_detects_locales_across_multiple_profiles(self, tmp_path):
        _make_props(str(tmp_path), ["messages.properties", "messages_de.properties"])
        json_en = tmp_path / "en" / "common.json"
        json_de = tmp_path / "de" / "common.json"
        json_en.parent.mkdir()
        json_de.parent.mkdir()
        json_en.write_text('{"hello":"Hello"}\n', encoding="utf-8")
        json_de.write_text('{"hello":"Hallo"}\n', encoding="utf-8")

        codes = [
            loc["code"]
            for loc in detect_locales_for_profiles(
                str(tmp_path),
                source_locale="en",
                localization_profiles=[
                    ("java_properties", "suffix"),
                    ("json", {"id": "locale_directory", "source_locale": "en"}),
                ],
            )
        ]

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
        assert cfg["localization_format"] == "java_properties"
        assert cfg["localization_layout"] == {"id": "suffix", "source_locale": "en"}

    def test_accepts_json_localization_format(self):
        cfg = build_config(
            target_project_root="/repo",
            input_folder="i18n",
            locales=[{"code": "de", "name": "German"}],
            localization_format="json",
        )
        assert cfg["localization_format"] == "json"

    def test_accepts_localization_layout(self):
        cfg = build_config(
            target_project_root="/repo",
            input_folder="i18n",
            locales=[{"code": "de", "name": "German"}],
            localization_format="json",
            localization_layout={"id": "locale_directory", "source_locale": "en"},
        )

        assert cfg["localization_layout"] == {"id": "locale_directory", "source_locale": "en"}

    def test_optional_base_url_included_when_provided(self):
        cfg = build_config(
            target_project_root="/repo", input_folder="i18n", locales=[],
            api_base_url="http://localhost:11434/v1",
        )
        assert cfg["api_base_url"] == "http://localhost:11434/v1"

    def test_no_base_url_key_when_not_provided(self):
        cfg = build_config(target_project_root="/repo", input_folder="i18n", locales=[])
        assert "api_base_url" not in cfg

    def test_accepts_multiple_localization_profiles(self):
        cfg = build_config(
            target_project_root="/repo",
            input_folder="i18n",
            locales=[{"code": "de", "name": "German"}],
            localization_profiles=[
                ("java_properties", "suffix"),
                ("json", {"id": "locale_directory", "source_locale": "en"}),
            ],
        )

        assert "localization_format" not in cfg
        assert cfg["localization_formats"] == [
            {"id": "java_properties", "layout": {"id": "suffix", "source_locale": "en"}},
            {"id": "json", "layout": {"id": "locale_directory", "source_locale": "en"}},
        ]


class TestProfileSpecParsing:
    def test_parse_profile_spec_uses_format_and_layout(self):
        fmt, layout = parse_localization_profile_spec("json:locale_directory", source_locale="en")

        assert fmt.id == "json"
        assert layout.id == "locale_directory"

    def test_parse_profile_spec_rejects_missing_layout(self):
        with pytest.raises(ValueError, match="FORMAT:LAYOUT"):
            parse_localization_profile_spec("json", source_locale="en")


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
        import localize.init_config as init_config

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

    def test_main_scaffolds_mixed_profile_config(self, tmp_path):
        import localize.init_config as init_config

        _make_props(str(tmp_path), ["messages.properties", "messages_de.properties"])
        json_en = tmp_path / "en" / "common.json"
        json_de = tmp_path / "de" / "common.json"
        json_en.parent.mkdir()
        json_de.parent.mkdir()
        json_en.write_text('{"hello":"Hello"}\n', encoding="utf-8")
        json_de.write_text('{"hello":"Hallo"}\n', encoding="utf-8")
        output = tmp_path / "config.yaml"

        rc = init_config.main([
            "--input-folder",
            str(tmp_path),
            "--output",
            str(output),
            "--localization-profile",
            "java_properties:suffix",
            "--localization-profile",
            "json:locale_directory",
        ])

        config = yaml.safe_load(output.read_text(encoding="utf-8"))
        assert rc == 0
        assert "localization_format" not in config
        assert [profile["id"] for profile in config["localization_formats"]] == [
            "java_properties",
            "json",
        ]
        assert config["supported_locales"] == [{"code": "de", "name": "German"}]
