import unittest
import os
import tempfile
import textwrap

# To be created
from src.translate_localization_files import lint_properties_file

class TestValidationLogic(unittest.TestCase):

    def test_linting_finds_common_errors(self):
        """
        Tests that our linter can detect common errors found in PR reviews:
        1. Malformed keys with double dots (..).
        2. Invalid Java escape sequences (e.g., \\U).
        """
        # Create a properties file with known errors
        bad_content = textwrap.dedent("""
            # This key is malformed
            key.one..bad=Some value

            # This key is correct
            key.two.good=Another value

            # This value has a bad escape sequence
            key.three.bad.escape=\\n\\Usando Tor externo
        """)

        errors = []
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(bad_content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(len(errors), 2, "Linter should have found exactly 2 errors.")

        # Check for specific error messages
        self.assertTrue(any("Malformed key 'key.one..bad'" in e for e in errors))
        self.assertTrue(any("Invalid escape sequence in value for key 'key.three.bad.escape'" in e for e in errors))

    def test_linting_detects_malformed_suppress_comment(self):
        """
        Regression test for the malformed suppress comment issue found in
        CodeRabbitAI reviews on bisq-mobile PRs #1098, #1102, #1107, #1111, #1117.

        The pipeline was generating `# suppress inspection "UnusedProperty`
        (missing closing quote) instead of `# suppress inspection "UnusedProperty"`.
        """
        content = (
            '# suppress inspection "UnusedProperty\n'
            'REVOLUT_SHORT=Revolut\n'
            '# suppress inspection "UnusedProperty"\n'
            'ZELLE_SHORT=Zelle\n'
        )

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(len(errors), 1)
        self.assertIn("Malformed suppress comment", errors[0])
        self.assertIn("line 1", errors[0])

    def test_linting_accepts_valid_suppress_comment(self):
        """Well-formed suppress comments must not trigger errors."""
        content = (
            '# suppress inspection "UnusedProperty"\n'
            'REVOLUT_SHORT=Revolut\n'
        )

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(errors, [])

    def test_linting_detects_multiple_malformed_suppress_comments(self):
        """Each malformed suppress comment should produce its own error."""
        content = (
            '# suppress inspection "UnusedProperty\n'
            'KEY_A=Alpha\n'
            '# suppress inspection "UnusedProperty\n'
            'KEY_B=Bravo\n'
            '# suppress inspection "UnusedProperty"\n'
            'KEY_C=Charlie\n'
        )

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(len(errors), 2)
        self.assertIn("line 1", errors[0])
        self.assertIn("line 3", errors[1])

    def test_linting_detects_control_character_unicode_escape_artifact(self):
        content = "learn.more=How this works " r"\u007f2192" "\n"

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(len(errors), 1)
        self.assertIn("Disallowed control character", errors[0])
        self.assertIn("learn.more", errors[0])
        self.assertIn("U+007F", errors[0])

    def test_linting_detects_actual_control_character_artifact(self):
        content = "learn.more=How this works \x7f2192\n"

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(len(errors), 1)
        self.assertIn("Disallowed control character", errors[0])
        self.assertIn("learn.more", errors[0])
        self.assertIn("U+007F", errors[0])

    def test_linting_accepts_utf8_arrow_glyph(self):
        content = "learn.more=How this works →\n"

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.properties', encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            errors = lint_properties_file(temp_path)
        finally:
            os.remove(temp_path)

        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
