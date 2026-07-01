"""Unit tests for the app_config module."""
import os
from unittest.mock import patch, mock_open, MagicMock

import pytest
import yaml

from localize.app_config import AppConfig, _non_empty_env, load_app_config, validate_config
from localize.localization_formats import JSON_FORMAT, JAVA_PROPERTIES_FORMAT
from localize.localization_layouts import SUFFIX_LAYOUT


class TestAppConfig:
    """Test cases for the AppConfig dataclass."""

    def test_app_config_creation(self):
        """Test that AppConfig can be created with required fields."""
        config = AppConfig(
            project_root="/test/root",
            target_project_root="/test/target",
            input_folder="/test/input",
            glossary_file_path="/test/glossary.json",
            model_name="gpt-4",
            review_model_name="gpt-4o",
            max_model_tokens=4000,
            dry_run=False,
            process_all_files=False,
            holistic_review_chunk_size=75,
            max_concurrent_api_calls=1,
            language_codes={"de": "German"},
            name_to_code={"german": "de"},
            retranslate_identical_source_strings=False,
            style_rules={},
            precomputed_style_rules_text={},
            brand_glossary=["Bisq"],
            project_context="A desktop trading app.",
            localization_format=JAVA_PROPERTIES_FORMAT,
            localization_layout=SUFFIX_LAYOUT,
            translation_queue_folder="/tmp/queue",
            translated_queue_folder="/tmp/translated",
            translation_key_ledger_file_path="/tmp/ledger.json",
            translation_memory_file_path="/tmp/memory.json",
            translation_memory_enabled=True,
            preserve_queues_for_debug=False,
            model_provider=None,
            openai_client=None
        )

        assert config.project_root == "/test/root"
        assert config.model_name == "gpt-4"
        assert config.dry_run is False
        assert config.language_codes == {"de": "German"}
        assert config.project_context == "A desktop trading app."
        assert config.localization_format == JAVA_PROPERTIES_FORMAT
        assert config.localization_layout == SUFFIX_LAYOUT
        assert config.localization_profiles[0].localization_format == JAVA_PROPERTIES_FORMAT


class TestLoadAppConfig:
    """Test cases for the load_app_config function."""

    def test_load_config_with_valid_yaml_file(self):
        """Test loading configuration from a valid YAML file."""
        mock_config = {
            "target_project_root": "/custom/target",
            "input_folder": "/custom/input",
            "model_name": "gpt-4o-mini",
            "dry_run": True,
            "retranslate_identical_source_strings": True,
            "translation_key_ledger_file_path": "/tmp/test-ledger.json",
            "translation_memory_file_path": "/tmp/test-memory.json",
            "translation_memory_enabled": False,
            "quality_gate": {
                "source_identical_min_block_count": 6,
                "source_identical_max_count": 25,
                "source_identical_max_ratio": 0.4,
                "block_on_pipeline_warnings": False,
                "block_on_semantic_qa_findings": "false",
                "block_on_semantic_qa_warnings": "yes",
                "semantic_qa_audit_scope": "all",
                "retained_source_word_allowlist": {
                    "fr": ["information", "message"],
                    "it": "reporting",
                },
            },
            "supported_locales": [
                {"code": "de", "name": "German"},
                {"code": "es", "name": "Spanish"}
            ],
            "logging": {
                "log_level": "DEBUG",
                "log_file_path": "test.log"
            }
        }

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {}, clear=True):
                            config = load_app_config()

        assert config.target_project_root == "/custom/target"
        assert config.input_folder == "/custom/input"
        assert config.model_name == "gpt-4o-mini"
        assert config.dry_run is True
        assert config.retranslate_identical_source_strings is True
        assert config.translation_key_ledger_file_path == "/tmp/test-ledger.json"
        assert config.translation_memory_file_path == "/tmp/test-memory.json"
        assert config.translation_memory_enabled is False
        assert config.quality_gate.source_identical_min_block_count == 6
        assert config.quality_gate.source_identical_max_count == 25
        assert config.quality_gate.source_identical_max_ratio == 0.4
        assert config.quality_gate.block_on_pipeline_warnings is False
        assert config.quality_gate.block_on_semantic_qa_findings is False
        assert config.quality_gate.block_on_semantic_qa_warnings is True
        assert config.quality_gate.semantic_qa_audit_scope == "all"
        assert config.quality_gate.retained_source_word_allowlist == {
            "fr": ("information", "message"),
            "it": ("reporting",),
        }
        assert config.language_codes == {"de": "German", "es": "Spanish"}
        assert config.name_to_code == {"german": "de", "spanish": "es"}

    def test_load_config_with_missing_file_uses_defaults(self):
        """Test that missing config file results in default values."""
        # Mock config.get calls to return default values, with dry_run=True to avoid API key requirement
        mock_config = {"dry_run": True}

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        # Should use default values
        assert config.model_name == "gpt-4"
        assert config.dry_run is True
        assert config.holistic_review_chunk_size == 30  # Updated from 75 to 30
        assert config.max_concurrent_api_calls == 1
        assert config.process_all_files is False
        assert config.retranslate_identical_source_strings is False
        assert config.quality_gate.source_identical_min_block_count == 5
        assert config.quality_gate.source_identical_max_count == 20
        assert config.quality_gate.source_identical_max_ratio == 0.30
        assert config.quality_gate.block_on_pipeline_warnings is True
        assert config.quality_gate.block_on_semantic_qa_findings is True
        assert config.quality_gate.block_on_semantic_qa_warnings is False
        assert config.quality_gate.semantic_qa_audit_scope == "changed"
        assert config.quality_gate.retained_source_word_allowlist == {}
        assert config.brand_glossary == []
        assert config.project_context == ""
        assert config.localization_format == JAVA_PROPERTIES_FORMAT
        assert config.localization_layout == SUFFIX_LAYOUT
        assert config.translation_key_ledger_file_path == os.path.join(
            config.project_root, "logs", "translation_key_ledger.json"
        )
        assert config.translation_memory_file_path == os.path.join(
            config.project_root, "logs", "translation_memory.json"
        )
        assert config.translation_memory_enabled is True

    def test_load_config_resolves_queue_folders_without_creating_them(self, tmp_path):
        mock_config = {"dry_run": True}

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("localize.app_config.tempfile.gettempdir", return_value=str(tmp_path)):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert config.translation_queue_folder == os.path.join(str(tmp_path), "translation_queue")
        assert config.translated_queue_folder == os.path.join(str(tmp_path), "translated_queue")
        assert not os.path.exists(config.translation_queue_folder)
        assert not os.path.exists(config.translated_queue_folder)


    def test_load_config_reads_project_context_and_format(self):
        mock_config = {
            "dry_run": True,
            "project_context": "Translate for a developer tool.",
            "brand_technical_glossary": ["Acme", "API"],
            "localization_format": {
                "id": "custom_json",
                "display_name": "Custom JSON",
                "file_extension": ".json",
                "code_fence": "json",
                "locale_suffix_regex": r"_([a-z]{2})\.json$",
            },
            "localization_layout": {
                "id": "locale_directory",
                "source_locale": "en",
            },
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert config.project_context == "Translate for a developer tool."
        assert config.brand_glossary == ["Acme", "API"]
        assert config.localization_format.id == "custom_json"
        assert config.localization_format.file_extension == ".json"
        assert config.localization_layout.id == "locale_directory"
        assert config.localization_layout.source_locale == "en"
        assert config.localization_profiles[0].localization_format.id == "custom_json"

    def test_load_config_reads_multiple_localization_profiles(self):
        mock_config = {
            "dry_run": True,
            "localization_formats": [
                {"id": "java_properties", "layout": "suffix"},
                {
                    "id": "json",
                    "layout": {"id": "locale_directory", "source_locale": "en"},
                },
            ],
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert [profile.localization_format for profile in config.localization_profiles] == [
            JAVA_PROPERTIES_FORMAT,
            JSON_FORMAT,
        ]
        assert config.localization_format == JAVA_PROPERTIES_FORMAT
        assert config.localization_profiles[1].localization_layout.id == "locale_directory"

    def test_load_config_with_invalid_format_falls_back_to_java_properties(self):
        mock_config = {
            "dry_run": True,
            "localization_format": "unknown_format",
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert config.localization_format == JAVA_PROPERTIES_FORMAT

    def test_load_config_with_invalid_multi_profile_raises(self):
        mock_config = {
            "dry_run": True,
            "localization_formats": [
                {"id": "unknown_format", "layout": "suffix"},
            ],
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        with pytest.raises(ValueError, match="Unsupported localization_format"):
                            load_app_config()

    def test_load_config_with_invalid_layout_preserves_source_locale(self):
        mock_config = {
            "dry_run": True,
            "source_locale": "fr",
            "localization_layout": "unknown_layout",
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert config.localization_layout.id == "suffix"
        assert config.localization_layout.source_locale == "fr"

    def test_load_config_treats_null_brand_glossary_as_empty(self):
        mock_config = {
            "dry_run": True,
            "brand_technical_glossary": None,
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert config.brand_glossary == []

    def test_load_config_with_process_all_files_enabled(self):
        """Test process_all_files can be enabled via config."""
        mock_config = {
            "dry_run": True,
            "process_all_files": True
        }

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {}, clear=True):
                        config = load_app_config()

        assert config.process_all_files is True

    def test_process_all_files_env_override(self):
        """PROCESS_ALL_FILES env var overrides the config value (used by the Action)."""
        mock_config = {"dry_run": True, "process_all_files": False}

        with patch("localize.app_config._load_yaml_config", return_value=mock_config):
            with patch("os.path.exists", return_value=False):
                with patch("localize.app_config.setup_logger") as mock_logger:
                    mock_logger.return_value = MagicMock()
                    with patch.dict(os.environ, {"PROCESS_ALL_FILES": "true"}, clear=True):
                        config = load_app_config()

        assert config.process_all_files is True

    def test_load_config_with_environment_overrides(self):
        """Test that environment variables override config file values."""
        mock_config = {"model_name": "gpt-4", "dry_run": True}

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {
                            "REVIEW_MODEL_NAME": "gpt-4o",
                            "HOLISTIC_REVIEW_CHUNK_SIZE": "100"
                        }):
                            config = load_app_config()

        assert config.model_name == "gpt-4"  # From config file
        assert config.review_model_name == "gpt-4o"  # From environment
        assert config.holistic_review_chunk_size == 100  # From environment

    def test_load_config_with_dotenv_file(self):
        """Test that .env file is loaded properly."""
        mock_config = {"model_name": "gpt-4", "dry_run": True}

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists") as mock_exists:
                # Mock .env file exists in project root
                mock_exists.side_effect = lambda path: path.endswith("/.env") or path.endswith("config.yaml")
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.load_dotenv") as mock_load_dotenv:
                        with patch("localize.app_config.setup_logger") as mock_logger:
                            mock_logger.return_value = MagicMock()
                            with patch.dict(os.environ, {}, clear=True):
                                load_app_config()

                # Should have called load_dotenv
                mock_load_dotenv.assert_called_once()

    def test_openai_client_creation_with_api_key(self):
        """Test that OpenAI client is created when API key is present."""
        mock_config = {"dry_run": False, "model_provider": "openai_compatible"}

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch("localize.model_provider.AsyncOpenAI") as mock_openai:
                            mock_client = MagicMock()
                            mock_openai.return_value = mock_client
                            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
                                config = load_app_config()

        mock_openai.assert_called_once_with(api_key="sk-test-key")
        assert config.openai_client == mock_client

    def test_openai_client_none_in_dry_run(self):
        """Test that OpenAI client is None in dry run mode."""
        mock_config = {"dry_run": True}

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                            config = load_app_config()

        assert config.openai_client is None

    def test_localize_dry_run_env_overrides_config(self):
        """CLI dry-run mode must force dry_run even when config is false."""
        with patch("builtins.open", mock_open(read_data=yaml.dump({"dry_run": False}))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.load_dotenv"):
                        with patch("localize.app_config.setup_logger") as mock_logger:
                            mock_logger.return_value = MagicMock()
                            with patch.dict(os.environ, {"LOCALIZE_DRY_RUN": "true"}, clear=True):
                                config = load_app_config()

        assert config.dry_run is True
        assert config.openai_client is None
        assert config.model_provider is None

    def test_false_localize_dry_run_env_does_not_disable_config_dry_run(self):
        with patch("builtins.open", mock_open(read_data=yaml.dump({"dry_run": True}))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.load_dotenv"):
                        with patch("localize.app_config.setup_logger") as mock_logger:
                            mock_logger.return_value = MagicMock()
                            with patch.dict(os.environ, {"LOCALIZE_DRY_RUN": "false"}, clear=True):
                                config = load_app_config()

        assert config.dry_run is True
        assert config.model_provider is None

    def test_load_config_rejects_non_mapping_aisuite_section(self):
        with patch("builtins.open", mock_open(read_data=yaml.dump({
            "dry_run": False,
            "aisuite": ["provider_configs"],
        }))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.load_dotenv"):
                        with patch("localize.app_config.setup_logger") as mock_logger:
                            mock_logger.return_value = MagicMock()
                            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}, clear=True):
                                with pytest.raises(SystemExit):
                                    load_app_config()

    def test_missing_openai_key_exits_in_production_mode(self):
        """Test that missing OpenAI API key causes system exit in production mode."""
        mock_config = {"dry_run": False, "model_provider": "openai_compatible"}

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.load_dotenv"):
                        with patch("localize.app_config.setup_logger") as mock_logger:
                            mock_logger.return_value = MagicMock()
                            with patch.dict(os.environ, {}, clear=True):
                                with pytest.raises(SystemExit):
                                    load_app_config()

    def test_style_rules_preprocessing(self):
        """Test that style rules are properly preprocessed."""
        mock_config = {
            "dry_run": True,  # Add dry_run to avoid OpenAI key requirement
            "supported_locales": [
                {"code": "de", "name": "German"}
            ],
            "style_rules": {
                "de": ["Rule 1", "Rule 2"]
            }
        }

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {}, clear=True):
                            config = load_app_config()

        assert "de" in config.precomputed_style_rules_text
        assert "German" in config.precomputed_style_rules_text["de"]
        assert "Rule 1" in config.precomputed_style_rules_text["de"]
        assert "Rule 2" in config.precomputed_style_rules_text["de"]

    def test_hyphenated_locale_codes(self):
        """Test that hyphenated locale codes like zh-Hans and zh-Hant are handled correctly."""
        mock_config = {
            "dry_run": True,
            "supported_locales": [
                {"code": "zh-Hans", "name": "Simplified Chinese"},
                {"code": "zh-Hant", "name": "Traditional Chinese"},
                {"code": "pt_BR", "name": "Brazilian Portuguese"}
            ],
            "style_rules": {
                "zh-Hans": ["Use simplified characters"],
                "zh-Hant": ["Use traditional characters"]
            }
        }

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {}, clear=True):
                            config = load_app_config()

        # Verify hyphenated codes are in language_codes dictionary
        assert "zh-Hans" in config.language_codes
        assert config.language_codes["zh-Hans"] == "Simplified Chinese"
        assert "zh-Hant" in config.language_codes
        assert config.language_codes["zh-Hant"] == "Traditional Chinese"

        # Verify name_to_code mappings work
        assert config.name_to_code["simplified chinese"] == "zh-Hans"
        assert config.name_to_code["traditional chinese"] == "zh-Hant"

        # Verify underscore codes still work
        assert "pt_BR" in config.language_codes
        assert config.language_codes["pt_BR"] == "Brazilian Portuguese"

        # Verify style rules are precomputed correctly
        assert "zh-Hans" in config.precomputed_style_rules_text
        assert "simplified characters" in config.precomputed_style_rules_text["zh-Hans"]
        assert "zh-Hant" in config.precomputed_style_rules_text
        assert "traditional characters" in config.precomputed_style_rules_text["zh-Hant"]

    def test_custom_config_file_path(self):
        """Test using custom config file path via environment variable."""
        mock_config = {"model_name": "custom-model", "dry_run": True}

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {"TRANSLATOR_CONFIG_FILE": "/custom/config.yaml"}):
                            config = load_app_config()

        assert config.model_name == "custom-model"


class TestValidateConfig:
    """Friendly, actionable validation of the raw config dict (pure function)."""

    GOOD = {
        "target_project_root": "/repo",
        "input_folder": "/repo/i18n",
        "supported_locales": [{"code": "de", "name": "German"}],
    }

    @staticmethod
    def _errors(issues):
        return [i.message for i in issues if i.level == "error"]

    @staticmethod
    def _warnings(issues):
        return [i.message for i in issues if i.level == "warning"]

    def test_valid_config_has_no_errors(self):
        issues = validate_config(self.GOOD, path_exists=lambda p: True)
        assert self._errors(issues) == []

    def test_missing_required_paths_are_errors(self):
        issues = validate_config({"supported_locales": self.GOOD["supported_locales"]},
                                 path_exists=lambda p: True)
        errs = self._errors(issues)
        assert any("target_project_root" in e for e in errs)
        assert any("input_folder" in e for e in errs)

    def test_placeholder_paths_are_errors(self):
        cfg = {**self.GOOD,
               "target_project_root": "/path/to/your/git/repo",
               "input_folder": "/path/to/properties/files"}
        errs = self._errors(validate_config(cfg, path_exists=lambda p: True))
        assert any("placeholder" in e.lower() for e in errs)

    def test_nonexistent_path_is_error(self):
        errs = self._errors(validate_config(self.GOOD, path_exists=lambda p: False))
        assert any("does not exist" in e for e in errs)

    def test_relative_input_folder_resolved_against_target_root(self):
        """A relative input_folder is resolved against target_project_root, like the shell does."""
        cfg = {"target_project_root": "/repo", "input_folder": "i18n",
               "supported_locales": [{"code": "de", "name": "German"}]}
        checked = []

        def fake_exists(p):
            checked.append(p)
            return p in ("/repo", os.path.join("/repo", "i18n"))

        issues = validate_config(cfg, path_exists=fake_exists)
        assert self._errors(issues) == []
        assert os.path.join("/repo", "i18n") in checked  # not the cwd-relative "i18n"

    def test_absolute_input_folder_checked_as_is(self):
        cfg = {"target_project_root": "/repo", "input_folder": "/elsewhere/i18n",
               "supported_locales": [{"code": "de", "name": "German"}]}
        checked = []

        def fake_exists(p):
            checked.append(p)
            return True

        validate_config(cfg, path_exists=fake_exists)
        assert "/elsewhere/i18n" in checked

    def test_no_locales_is_warning(self):
        cfg = {"target_project_root": "/repo", "input_folder": "/repo/i18n"}
        warns = self._warnings(validate_config(cfg, path_exists=lambda p: True))
        assert any("supported_locales" in w for w in warns)

    def test_style_rule_for_unknown_locale_is_warning(self):
        cfg = {**self.GOOD, "style_rules": {"de": ["ok"], "xx": ["typo"]}}
        warns = self._warnings(validate_config(cfg, path_exists=lambda p: True))
        assert any("xx" in w for w in warns)
        # Known locale must not be flagged.
        assert not any("'de'" in w for w in warns)

    def test_bad_api_base_url_scheme_is_warning(self):
        cfg = {**self.GOOD, "api_base_url": "localhost:11434/v1"}
        warns = self._warnings(validate_config(cfg, path_exists=lambda p: True))
        assert any("api_base_url" in w for w in warns)

    def test_good_api_base_url_no_warning(self):
        cfg = {**self.GOOD, "api_base_url": "http://localhost:11434/v1"}
        warns = self._warnings(validate_config(cfg, path_exists=lambda p: True))
        assert not any("api_base_url" in w for w in warns)

    def test_effective_base_url_overrides_config_for_validation(self):
        """A bad OPENAI_BASE_URL override is validated even if the config value is fine."""
        cfg = {**self.GOOD, "api_base_url": "http://localhost:11434/v1"}
        warns = self._warnings(validate_config(
            cfg, path_exists=lambda p: True, effective_api_base_url="localhost:bad"))
        assert any("api_base_url" in w for w in warns)

    def test_effective_base_url_valid_no_warning(self):
        cfg = {**self.GOOD, "api_base_url": "not-a-url"}  # config value bad...
        warns = self._warnings(validate_config(
            cfg, path_exists=lambda p: True,
            effective_api_base_url="https://api.example.com/v1"))  # ...but effective is good
        assert not any("api_base_url" in w for w in warns)

    def test_malformed_sections_do_not_crash(self):
        cfg = {"target_project_root": "/repo", "input_folder": "/repo/i18n",
               "supported_locales": "not-a-list", "style_rules": "not-a-dict"}
        # Should not raise.
        issues = validate_config(cfg, path_exists=lambda p: True)
        assert isinstance(issues, list)

    def test_real_openai_backed_run_without_key_is_error(self):
        cfg = {**self.GOOD, "dry_run": False, "model_name": "gpt-4o-mini"}
        errs = self._errors(validate_config(
            cfg,
            path_exists=lambda p: True,
            api_key_available=False,
        ))
        assert any("OPENAI_API_KEY" in e for e in errs)

    def test_dry_run_without_key_is_valid_for_preflight(self):
        cfg = {**self.GOOD, "dry_run": True, "model_name": "gpt-4o-mini"}
        errs = self._errors(validate_config(
            cfg,
            path_exists=lambda p: True,
            api_key_available=False,
        ))
        assert not any("OPENAI_API_KEY" in e for e in errs)

    def test_non_openai_aisuite_route_without_openai_key_is_valid_for_preflight(self):
        cfg = {
            **self.GOOD,
            "dry_run": False,
            "model_name": "anthropic:claude-3-5-sonnet-latest",
            "review_model_name": "anthropic:claude-3-5-sonnet-latest",
            "aisuite": {"provider_configs": {"anthropic": {"api_key": "secret"}}},
        }
        errs = self._errors(validate_config(
            cfg,
            path_exists=lambda p: True,
            api_key_available=False,
        ))
        assert not any("OPENAI_API_KEY" in e for e in errs)

    def test_malformed_aisuite_provider_config_is_validation_error(self):
        cfg = {
            **self.GOOD,
            "dry_run": False,
            "aisuite": {"provider_configs": {"openai": ["api_key", "secret"]}},
        }
        errs = self._errors(validate_config(
            cfg,
            path_exists=lambda p: True,
            api_key_available=False,
        ))
        assert any("aisuite.provider_configs.openai" in e for e in errs)

    def test_non_mapping_aisuite_section_is_validation_error(self):
        cfg = {**self.GOOD, "dry_run": False, "aisuite": ["provider_configs"]}
        errs = self._errors(validate_config(
            cfg,
            path_exists=lambda p: True,
            api_key_available=False,
        ))
        assert any("'aisuite' must be a mapping" in e for e in errs)

    def test_malformed_aisuite_provider_config_is_error_with_custom_endpoint(self):
        cfg = {
            **self.GOOD,
            "dry_run": False,
            "api_base_url": "http://localhost:11434/v1",
            "aisuite": {"provider_configs": {"openai": ["api_key", "secret"]}},
        }
        errs = self._errors(validate_config(
            cfg,
            path_exists=lambda p: True,
            api_key_available=False,
        ))
        assert any("aisuite.provider_configs.openai" in e for e in errs)


class TestProviderAbstraction:
    """Provider abstraction: OpenAI-compatible base_url / BYO-key / local (Ollama)."""

    @staticmethod
    def _load(mock_config, env):
        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch("localize.model_provider.AsyncOpenAI") as mock_openai:
                            mock_openai.return_value = MagicMock()
                            with patch.dict(os.environ, env, clear=True):
                                config = load_app_config()
        return config, mock_openai

    def test_aisuite_is_default_provider(self):
        """AISuite is the default abstraction layer for chat model dispatch."""
        provider = MagicMock()
        provider.client = object()

        with patch("builtins.open", mock_open(read_data=yaml.dump({"dry_run": False}))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch("localize.app_config.create_model_provider", return_value=provider) as factory:
                            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}, clear=True):
                                config = load_app_config()

        assert config.model_provider_name == "aisuite"
        _, kwargs = factory.call_args
        assert kwargs["provider_name"] == "aisuite"
        assert kwargs["model_names"] == ("gpt-4", "gpt-4")
        assert config.model_provider is provider

    def test_default_aisuite_missing_openai_key_exits_for_openai_models(self):
        with patch("builtins.open", mock_open(read_data=yaml.dump({"dry_run": False}))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.load_dotenv"):
                        with patch("localize.app_config.setup_logger") as mock_logger:
                            mock_logger.return_value = MagicMock()
                            with patch.dict(os.environ, {}, clear=True):
                                with pytest.raises(SystemExit):
                                    load_app_config()

    def test_model_provider_name_is_canonicalized(self):
        provider = MagicMock()
        provider.client = object()

        with patch("builtins.open", mock_open(read_data=yaml.dump(
            {"dry_run": False, "model_provider": "openai"}
        ))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch("localize.app_config.create_model_provider", return_value=provider) as factory:
                            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}, clear=True):
                                config = load_app_config()

        assert config.model_provider_name == "openai_compatible"
        assert factory.call_args.kwargs["provider_name"] == "openai_compatible"

    def test_base_url_from_config_passed_to_client(self):
        """api_base_url in config is passed to the OpenAI-compatible client."""
        config, mock_openai = self._load(
            {
                "dry_run": False,
                "model_provider": "openai_compatible",
                "api_base_url": "http://localhost:11434/v1",
            },
            {"OPENAI_API_KEY": "sk-test-key"},
        )
        assert config.api_base_url == "http://localhost:11434/v1"
        _, kwargs = mock_openai.call_args
        assert kwargs["base_url"] == "http://localhost:11434/v1"
        assert kwargs["api_key"] == "sk-test-key"

    def test_base_url_env_overrides_config(self):
        """OPENAI_BASE_URL env var wins over the config value."""
        config, mock_openai = self._load(
            {
                "dry_run": False,
                "model_provider": "openai_compatible",
                "api_base_url": "http://config-host/v1",
            },
            {"OPENAI_API_KEY": "sk-test-key", "OPENAI_BASE_URL": "http://env-host/v1"},
        )
        assert config.api_base_url == "http://env-host/v1"
        _, kwargs = mock_openai.call_args
        assert kwargs["base_url"] == "http://env-host/v1"

    def test_empty_base_url_env_is_ignored(self):
        """A blank OPENAI_BASE_URL must not shadow the SDK default endpoint."""
        config, mock_openai = self._load(
            {
                "dry_run": False,
                "model_provider": "openai_compatible",
            },
            {"OPENAI_API_KEY": "sk-test-key", "OPENAI_BASE_URL": ""},
        )
        assert config.api_base_url is None
        _, kwargs = mock_openai.call_args
        assert "base_url" not in kwargs

    def test_empty_env_value_is_removed_before_sdk_initialization(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "   "}, clear=True):
            assert _non_empty_env("OPENAI_BASE_URL") is None
            assert "OPENAI_BASE_URL" not in os.environ

    def test_empty_review_model_env_falls_back_to_config(self):
        config, _ = self._load(
            {
                "dry_run": False,
                "model_provider": "openai_compatible",
                "model_name": "gpt-4o-mini",
                "review_model_name": "gpt-4o-mini",
            },
            {"OPENAI_API_KEY": "sk-test-key", "REVIEW_MODEL_NAME": ""},
        )
        assert config.review_model_name == "gpt-4o-mini"

    def test_local_provider_without_key_uses_placeholder(self):
        """A custom endpoint (e.g. Ollama) needs no real key; we must not exit."""
        config, mock_openai = self._load(
            {
                "dry_run": False,
                "model_provider": "openai_compatible",
                "api_base_url": "http://localhost:11434/v1",
            },
            {},  # no OPENAI_API_KEY
        )
        _, kwargs = mock_openai.call_args
        assert kwargs["base_url"] == "http://localhost:11434/v1"
        assert kwargs["api_key"]  # non-empty placeholder so the SDK does not raise
        assert config.openai_client is not None

    def test_missing_key_without_base_url_still_exits(self):
        """Without a custom endpoint, a missing key is still a hard error."""
        with patch("builtins.open", mock_open(read_data=yaml.dump(
            {"dry_run": False, "model_provider": "openai_compatible"}
        ))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch.dict(os.environ, {}, clear=True):
                            with pytest.raises(SystemExit):
                                load_app_config()

    def test_non_sk_key_with_custom_endpoint_not_warned(self):
        """A BYO key on a custom endpoint may not start with sk-; accept it without warning."""
        captured = MagicMock()
        with patch("builtins.open", mock_open(read_data=yaml.dump(
            {
                "dry_run": False,
                "model_provider": "openai_compatible",
                "api_base_url": "https://api.groq.com/openai/v1",
            }
        ))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger", return_value=captured):
                        with patch("localize.model_provider.AsyncOpenAI", return_value=MagicMock()):
                            with patch.dict(os.environ, {"OPENAI_API_KEY": "gsk_abc123"}, clear=True):
                                load_app_config()
        warnings = [str(c) for c in captured.warning.call_args_list]
        assert not any("sk-" in w for w in warnings)

    def test_aisuite_backend_is_configurable(self):
        provider = MagicMock()
        provider.client = object()
        mock_config = {
            "dry_run": False,
            "model_provider": "aisuite",
            "aisuite": {
                "provider_configs": {
                    "anthropic": {"api_key": "from-env-or-secret"}
                }
            },
        }

        with patch("builtins.open", mock_open(read_data=yaml.dump(mock_config))):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    with patch("localize.app_config.setup_logger") as mock_logger:
                        mock_logger.return_value = MagicMock()
                        with patch("localize.app_config.create_model_provider", return_value=provider) as factory:
                            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
                                config = load_app_config()

        assert config.model_provider_name == "aisuite"
        assert config.model_provider is provider
        assert config.openai_client is provider.client
        _, kwargs = factory.call_args
        assert kwargs["provider_name"] == "aisuite"
        assert kwargs["aisuite_provider_configs"] == {
            "anthropic": {"api_key": "from-env-or-secret"}
        }
        assert kwargs["model_names"] == ("gpt-4", "gpt-4")
