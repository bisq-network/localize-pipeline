"""
Integration tests for the `translate_localization_files.py` script.

This test suite focuses on testing the overall workflow of the script by mocking
external dependencies and file system operations, and also includes unit-like tests
for specific helper functions within the script.
"""
import os
import json
from unittest.mock import AsyncMock, patch, MagicMock
from types import SimpleNamespace
import pytest
import src.translate_localization_files
from src.localization_formats import JSON_FORMAT, JAVA_PROPERTIES_FORMAT
from src.localization_layouts import LocalizationLayout
from src.localization_profiles import LocalizationProfile

# All fixtures are now defined in conftest.py and are auto-discovered by pytest.

@pytest.mark.asyncio
@patch('src.translate_localization_files.get_changed_translation_files')
@patch('src.translate_localization_files.copy_files_to_translation_queue')
@patch('src.translate_localization_files.process_translation_queue')
@patch('src.translate_localization_files.copy_translated_files_back')
@patch('src.translate_localization_files.move_files_to_archive')
async def test_main_flow_no_changes(mock_move, mock_copy_back, mock_process, mock_copy_to_queue, mock_get_changed, integration_test_environment):
    mock_get_changed.return_value = []
    await src.translate_localization_files.main()
    mock_get_changed.assert_called_once_with(
        src.translate_localization_files.INPUT_FOLDER,
        src.translate_localization_files.REPO_ROOT,
        process_all_files=src.translate_localization_files.PROCESS_ALL_FILES
    )
    mock_copy_to_queue.assert_not_called()
    mock_process.assert_not_called()
    mock_copy_back.assert_not_called()
    mock_move.assert_not_called()


@pytest.mark.asyncio
async def test_main_translates_json_from_detection_to_copyback(integration_test_environment):
    env = integration_test_environment
    source_file_path = os.path.join(env['input_folder'], 'app.json')
    target_file_path = os.path.join(env['input_folder'], 'app_de.json')

    with open(source_file_path, 'w', encoding='utf-8') as f:
        json.dump({"hello": "Hello", "nested": {"title": "Title {0}"}}, f, ensure_ascii=False, indent=2)
    with open(target_file_path, 'w', encoding='utf-8') as f:
        json.dump({"hello": "Hallo"}, f, ensure_ascii=False, indent=2)

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Titel {0}"))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"/nested/title": "Titel {0}"})
        ))]),
    ])
    provider.count_tokens.side_effect = lambda text, model: len(text.split())
    provider.estimate_run_cost.return_value = MagicMock()
    provider.format_estimate.return_value = "estimate"
    provider.is_retryable_error.return_value = False

    profiles = (
        LocalizationProfile(JSON_FORMAT, LocalizationLayout(id="suffix", source_locale="en")),
    )
    with patch('src.translate_localization_files.get_changed_translation_files',
               return_value=['app_de.json']), \
         patch('src.translate_localization_files.LOCALIZATION_FORMAT', JSON_FORMAT), \
         patch('src.translate_localization_files.LOCALIZATION_PROFILES', profiles), \
         patch('src.translate_localization_files.LANGUAGE_CODES', {'de': 'German'}), \
         patch('src.translate_localization_files.MODEL_PROVIDER', provider), \
         patch('src.translate_localization_files.PRESERVE_QUEUES_FOR_DEBUG', True), \
         patch('src.translate_localization_files.TRANSLATION_KEY_LEDGER_FILE_PATH',
               os.path.join(env['input_folder'], 'ledger.json')), \
         patch('src.translate_localization_files.get_working_tree_changed_keys', return_value=set()):
        await src.translate_localization_files.main()

    with open(target_file_path, 'r', encoding='utf-8') as f:
        final_payload = json.load(f)

    assert final_payload == {
        "hello": "Hallo",
        "nested": {"title": "Titel {0}"},
    }


@pytest.mark.asyncio
async def test_process_translation_queue_end_to_end(integration_test_environment):
    env = integration_test_environment
    source_content = "key.one=value one\nkey.two=value two"
    target_content = "key.one=Wert eins"  # This key is already translated
    source_file_path = os.path.join(env['input_folder'], 'app.properties')
    target_file_path = os.path.join(env['translation_queue_folder'], 'app_de.properties')

    with open(source_file_path, 'w', encoding='utf-8') as f:
        f.write(source_content)
    with open(target_file_path, 'w', encoding='utf-8') as f:
        f.write(target_content)

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Wert zwei"))]
    ))
    provider.count_tokens.side_effect = lambda text, model: len(text.split())
    provider.estimate_run_cost.return_value = MagicMock()
    provider.format_estimate.return_value = "estimate"
    provider.is_retryable_error.return_value = False

    with patch('src.translate_localization_files.MODEL_PROVIDER', provider):
        await src.translate_localization_files.process_translation_queue(
            translation_queue_folder=env['translation_queue_folder'],
            translated_queue_folder=env['translated_queue_folder'],
            glossary_file_path=env['mock_glossary_path_resolved']
        )

    output_file_path = os.path.join(env['translated_queue_folder'], 'app_de.properties')
    assert os.path.exists(output_file_path)
    with open(output_file_path, 'r', encoding='utf-8') as f:
        final_content = f.read()
        assert "key.two=Wert zwei" in final_content
        assert "key.one=Wert eins" in final_content
        assert len(final_content.strip().split('\n')) == 2

@pytest.mark.asyncio
async def test_handles_already_escaped_quotes_correctly(integration_test_environment):
    env = integration_test_environment
    source_content = "key.name=URL is ''{0}''"
    target_content = ""

    source_en_path = os.path.join(env['input_folder'], 'app.properties')
    target_de_path = os.path.join(env['translation_queue_folder'], 'app_de.properties')

    with open(source_en_path, 'w', encoding='utf-8') as f:
        f.write(source_content)
    with open(target_de_path, 'w', encoding='utf-8') as f:
        f.write(target_content)

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="URL ist '{0}'"))]
    ))
    provider.count_tokens.side_effect = lambda text, model: len(text.split())
    provider.estimate_run_cost.return_value = MagicMock()
    provider.format_estimate.return_value = "estimate"
    provider.is_retryable_error.return_value = False

    with patch('src.translate_localization_files.lint_properties_file', return_value=[]), \
         patch('src.translate_localization_files.MODEL_PROVIDER', provider):
        await src.translate_localization_files.process_translation_queue(
            translation_queue_folder=env['translation_queue_folder'],
            translated_queue_folder=env['translated_queue_folder'],
            glossary_file_path=env['mock_glossary_path_resolved']
        )

    output_file_path = os.path.join(env['translated_queue_folder'], 'app_de.properties')
    with open(output_file_path, 'r', encoding='utf-8') as f:
        final_content = f.read().strip()
        expected_content = "key.name=URL ist ''{0}''"
        assert final_content == expected_content
        assert "''''" not in final_content


@pytest.mark.asyncio
async def test_process_translation_queue_translates_json_locale_file(integration_test_environment):
    env = integration_test_environment
    source_content = {
        "hello": "Hello",
        "nested": {
            "title": "Title {0}",
        },
        "count": 3,
    }
    target_content = {
        "hello": "Hallo",
    }
    source_file_path = os.path.join(env['input_folder'], 'app.json')
    target_file_path = os.path.join(env['translation_queue_folder'], 'app_de.json')

    with open(source_file_path, 'w', encoding='utf-8') as f:
        json.dump(source_content, f, ensure_ascii=False, indent=2)
    with open(target_file_path, 'w', encoding='utf-8') as f:
        json.dump(target_content, f, ensure_ascii=False, indent=2)

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Titel {0}"))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"/nested/title": "Titel {0}"})
        ))]),
    ])
    provider.count_tokens.side_effect = lambda text, model: len(text.split())
    provider.estimate_run_cost.return_value = MagicMock()
    provider.format_estimate.return_value = "estimate"
    provider.is_retryable_error.return_value = False

    with patch('src.translate_localization_files.LOCALIZATION_FORMAT', JSON_FORMAT), \
         patch('src.translate_localization_files.LANGUAGE_CODES', {'de': 'German'}), \
         patch('src.translate_localization_files.MODEL_PROVIDER', provider), \
         patch('src.translate_localization_files.TRANSLATION_KEY_LEDGER_FILE_PATH',
               os.path.join(env['input_folder'], 'ledger.json')), \
         patch('src.translate_localization_files.get_working_tree_changed_keys', return_value=set()):
        await src.translate_localization_files.process_translation_queue(
            translation_queue_folder=env['translation_queue_folder'],
            translated_queue_folder=env['translated_queue_folder'],
            glossary_file_path=env['mock_glossary_path_resolved']
        )

    output_file_path = os.path.join(env['translated_queue_folder'], 'app_de.json')
    assert os.path.exists(output_file_path)
    with open(output_file_path, 'r', encoding='utf-8') as f:
        final_payload = json.load(f)

    assert final_payload == {
        "hello": "Hallo",
        "nested": {
            "title": "Titel {0}",
        },
        "count": 3,
    }


@pytest.mark.asyncio
async def test_process_translation_queue_translates_json_locale_directory_layout(integration_test_environment):
    env = integration_test_environment
    layout = LocalizationLayout(id="locale_directory", source_locale="en")
    source_content = {
        "hello": "Hello",
        "steps": [
            {"title": "Review details"},
        ],
    }
    target_content = {
        "hello": "Hallo",
    }
    source_file_path = os.path.join(env['input_folder'], 'en', 'app.json')
    target_file_path = os.path.join(env['translation_queue_folder'], 'de', 'app.json')
    os.makedirs(os.path.dirname(source_file_path), exist_ok=True)
    os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

    with open(source_file_path, 'w', encoding='utf-8') as f:
        json.dump(source_content, f, ensure_ascii=False, indent=2)
    with open(target_file_path, 'w', encoding='utf-8') as f:
        json.dump(target_content, f, ensure_ascii=False, indent=2)

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Details prüfen"))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"/steps/0/title": "Details prüfen"})
        ))]),
    ])
    provider.count_tokens.side_effect = lambda text, model: len(text.split())
    provider.estimate_run_cost.return_value = MagicMock()
    provider.format_estimate.return_value = "estimate"
    provider.is_retryable_error.return_value = False

    with patch('src.translate_localization_files.LOCALIZATION_FORMAT', JSON_FORMAT), \
         patch('src.translate_localization_files.LOCALIZATION_LAYOUT', layout), \
         patch('src.translate_localization_files.LANGUAGE_CODES', {'de': 'German'}), \
         patch('src.translate_localization_files.MODEL_PROVIDER', provider), \
         patch('src.translate_localization_files.TRANSLATION_KEY_LEDGER_FILE_PATH',
               os.path.join(env['input_folder'], 'ledger.json')), \
         patch('src.translate_localization_files.get_working_tree_changed_keys', return_value=set()):
        await src.translate_localization_files.process_translation_queue(
            translation_queue_folder=env['translation_queue_folder'],
            translated_queue_folder=env['translated_queue_folder'],
            glossary_file_path=env['mock_glossary_path_resolved']
        )

    output_file_path = os.path.join(env['translated_queue_folder'], 'de', 'app.json')
    assert os.path.exists(output_file_path)
    with open(output_file_path, 'r', encoding='utf-8') as f:
        final_payload = json.load(f)

    assert final_payload == {
        "hello": "Hallo",
        "steps": [
            {"title": "Details prüfen"},
        ],
    }


@pytest.mark.asyncio
async def test_process_translation_queue_routes_mixed_format_profiles(integration_test_environment):
    env = integration_test_environment
    properties_source_path = os.path.join(env['input_folder'], 'app.properties')
    properties_target_path = os.path.join(env['translation_queue_folder'], 'app_de.properties')
    json_source_path = os.path.join(env['input_folder'], 'locales', 'en', 'common.json')
    json_target_path = os.path.join(env['translation_queue_folder'], 'locales', 'de', 'common.json')
    os.makedirs(os.path.dirname(json_source_path), exist_ok=True)
    os.makedirs(os.path.dirname(json_target_path), exist_ok=True)

    with open(properties_source_path, 'w', encoding='utf-8') as f:
        f.write('headline=Headline\ncta=Continue with {0}\n')
    with open(properties_target_path, 'w', encoding='utf-8') as f:
        f.write('headline=Ueberschrift\n')
    with open(json_source_path, 'w', encoding='utf-8') as f:
        json.dump({
            "nested": {"title": "JSON title {0}"},
            "metadata": {"version": 1},
        }, f, ensure_ascii=False, indent=2)
    with open(json_target_path, 'w', encoding='utf-8') as f:
        json.dump({"metadata": {"version": 1}}, f, ensure_ascii=False, indent=2)

    async def fake_completion(**kwargs):
        if kwargs.get("response_format"):
            system_content = kwargs["messages"][0]["content"]
            if "/nested/title" in system_content:
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps({"/nested/title": "JSON-Titel {0}"})
                ))])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"cta": "Weiter mit {0}"})
            ))])

        user_content = kwargs["messages"][1]["content"]
        if "Key: /nested/title" in user_content:
            content = "JSON-Titel {0}"
        else:
            content = "Weiter mit {0}"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(side_effect=fake_completion)
    provider.count_tokens.side_effect = lambda text, model: len(text.split())
    provider.estimate_run_cost.return_value = MagicMock()
    provider.format_estimate.return_value = "estimate"
    provider.is_retryable_error.return_value = False

    profiles = (
        LocalizationProfile(
            JAVA_PROPERTIES_FORMAT,
            LocalizationLayout(id="suffix", source_locale="en"),
        ),
        LocalizationProfile(
            JSON_FORMAT,
            LocalizationLayout(id="locale_directory", source_locale="en"),
        ),
    )
    with patch('src.translate_localization_files.LOCALIZATION_PROFILES', profiles), \
         patch('src.translate_localization_files.LANGUAGE_CODES', {'de': 'German'}), \
         patch('src.translate_localization_files.MODEL_PROVIDER', provider), \
         patch('src.translate_localization_files.TRANSLATION_KEY_LEDGER_FILE_PATH',
               os.path.join(env['input_folder'], 'ledger.json')), \
         patch('src.translate_localization_files.get_working_tree_changed_keys', return_value=set()):
        await src.translate_localization_files.process_translation_queue(
            translation_queue_folder=env['translation_queue_folder'],
            translated_queue_folder=env['translated_queue_folder'],
            glossary_file_path=env['mock_glossary_path_resolved']
        )

    with open(os.path.join(env['translated_queue_folder'], 'app_de.properties'), 'r', encoding='utf-8') as f:
        properties_content = f.read()
    with open(os.path.join(env['translated_queue_folder'], 'locales', 'de', 'common.json'), 'r', encoding='utf-8') as f:
        json_payload = json.load(f)

    assert "headline=Ueberschrift" in properties_content
    assert "cta=Weiter mit {0}" in properties_content
    assert json_payload == {
        "nested": {"title": "JSON-Titel {0}"},
        "metadata": {"version": 1},
    }
