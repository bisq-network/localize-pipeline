import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

# Set a dummy API key before importing the main script to prevent SystemExit.
os.environ['OPENAI_API_KEY'] = 'DUMMY_KEY_FOR_TESTING'

from src.properties_parser import reassemble_file


class TestQuoteEscaping(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="test_quote_escaping_")
        self.test_dir = self.tmpdir.name
        self.queue_dir = os.path.join(self.test_dir, "queue")
        self.translated_dir = os.path.join(self.test_dir, "translated")
        os.makedirs(self.queue_dir, exist_ok=True)
        os.makedirs(self.translated_dir, exist_ok=True)
    
    def tearDown(self):
        self.tmpdir.cleanup()

    @patch('src.translate_localization_files.run_post_translation_validation')
    @patch('src.translate_localization_files.holistic_review_async', new_callable=AsyncMock)
    @patch('src.translate_localization_files.run_pre_translation_validation')
    @patch('src.translate_localization_files.load_glossary')
    @patch('src.translate_localization_files.get_working_tree_changed_keys')
    async def test_single_quotes_are_escaped(self, mock_git_changed_keys, mock_load_glossary, mock_pre_validator, mock_holistic_review, mock_post_validator):
        from src.translate_localization_files import process_translation_queue, LANGUAGE_CODES, NAME_TO_CODE, REPO_ROOT

        # Configure the mocks
        # The pre-validator returns (errors, newly_added_keys).
        # Mark the key as newly synchronized so it is translated in this run.
        mock_pre_validator.return_value = ([], {"test.key"})
        mock_post_validator.return_value = True # Post-validation is now mocked
        mock_holistic_review.return_value = None
        mock_load_glossary.return_value = {}  # Mock the glossary to be empty
        mock_git_changed_keys.return_value = set()

        # 1. Mock the AI response for the initial translation
        response_text = "Dies ist ein '{0}' Beispiel."
        provider = MagicMock()
        provider.create_chat_completion = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
        ))
        provider.count_tokens.side_effect = lambda text, model: len(text.split())
        provider.estimate_run_cost.return_value = MagicMock()
        provider.format_estimate.return_value = "estimate"
        provider.is_retryable_error.return_value = False

        # 2. Create the dummy files the function needs to find
        source_file_path = os.path.join(self.test_dir, 'app.properties')
        with open(source_file_path, 'w', encoding='utf-8') as f:
            f.write("test.key=This has a {0} placeholder.")
            
        target_file_path = os.path.join(self.queue_dir, 'app_de.properties')
        with open(target_file_path, 'w', encoding='utf-8') as f:
            f.write("test.key=This has a {0} placeholder.")

        # 3. Run the process, patching globals that are still read directly
        with patch.dict(LANGUAGE_CODES, {"de": "German"}), \
             patch.dict(NAME_TO_CODE, {"german": "de"}), \
             patch('src.translate_localization_files.INPUT_FOLDER', self.test_dir), \
             patch('src.translate_localization_files.MODEL_PROVIDER', provider):
            await process_translation_queue(
                translation_queue_folder=self.queue_dir,
                translated_queue_folder=self.translated_dir,
                glossary_file_path="dummy_path.json" # Path is mocked, content is controlled
            )

        # 4. Assertions
        mock_pre_validator.assert_called()
        mock_holistic_review.assert_awaited()
        # The AI should be called for the initial translation
        provider.create_chat_completion.assert_awaited()
        mock_git_changed_keys.assert_called_once_with(
            os.path.join(self.test_dir, 'app_de.properties'),
            REPO_ROOT
        )

        output_file_path = os.path.join(self.translated_dir, 'app_de.properties')
        self.assertTrue(os.path.exists(output_file_path))

        with open(output_file_path, 'r', encoding='utf-8') as f:
            output_content = f.read().strip()
        
        self.assertEqual(output_content, "test.key=Dies ist ein ''{0}'' Beispiel.")

    @patch('src.translate_localization_files.run_post_translation_validation')
    @patch('src.translate_localization_files.holistic_review_async', new_callable=AsyncMock)
    @patch('src.translate_localization_files.run_pre_translation_validation')
    @patch('src.translate_localization_files.load_glossary')
    @patch('src.translate_localization_files.get_working_tree_changed_keys')
    async def test_review_corrections_are_escaped_before_writing(
            self,
            mock_git_changed_keys,
            mock_load_glossary,
            mock_pre_validator,
            mock_holistic_review,
            mock_post_validator,
    ):
        from src.translate_localization_files import process_translation_queue, LANGUAGE_CODES, NAME_TO_CODE

        mock_pre_validator.return_value = ([], {"test.key"})
        mock_post_validator.return_value = True
        mock_holistic_review.return_value = {"test.key": "C'est le choix {0}."}
        mock_load_glossary.return_value = {}
        mock_git_changed_keys.return_value = set()

        provider = MagicMock()
        provider.create_chat_completion = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Brouillon {0}."))]
        ))
        provider.count_tokens.side_effect = lambda text, model: len(text.split())
        provider.estimate_run_cost.return_value = MagicMock()
        provider.format_estimate.return_value = "estimate"
        provider.is_retryable_error.return_value = False

        source_file_path = os.path.join(self.test_dir, 'app.properties')
        with open(source_file_path, 'w', encoding='utf-8') as f:
            f.write("test.key=This has a {0} placeholder.")

        target_file_path = os.path.join(self.queue_dir, 'app_fr.properties')
        with open(target_file_path, 'w', encoding='utf-8') as f:
            f.write("test.key=This has a {0} placeholder.")

        with patch.dict(LANGUAGE_CODES, {"fr": "French"}), \
             patch.dict(NAME_TO_CODE, {"french": "fr"}), \
             patch('src.translate_localization_files.INPUT_FOLDER', self.test_dir), \
             patch('src.translate_localization_files.MODEL_PROVIDER', provider):
            await process_translation_queue(
                translation_queue_folder=self.queue_dir,
                translated_queue_folder=self.translated_dir,
                glossary_file_path="dummy_path.json"
            )

        output_file_path = os.path.join(self.translated_dir, 'app_fr.properties')
        with open(output_file_path, 'r', encoding='utf-8') as f:
            output_content = f.read().strip()

        self.assertEqual(output_content, "test.key=C''est le choix {0}.")

    def test_reassemble_with_single_quotes(self):
        # This test only needs reassemble_file, no circular import.
        parsed_lines = [
            {'type': 'entry', 'key': 'key.one', 'value': "This is a value with 'quotes'", 'original_value': "This is a value with 'quotes'", 'line_number': 0}
        ]
        content = reassemble_file(parsed_lines)
        self.assertEqual(content, "key.one=This is a value with 'quotes'\n")
