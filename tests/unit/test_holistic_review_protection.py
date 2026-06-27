"""
Unit tests for holistic review placeholder protection.

Tests the ability to protect placeholders during holistic review phase
to prevent the AI from modifying, removing, or adding placeholders.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.translate_localization_files import (
    holistic_review_async,
    protect_placeholders_in_properties,
    restore_placeholders_in_properties
)


class TestHolisticReviewPlaceholderProtection:
    """Test suite for placeholder protection in holistic review."""

    def test_protect_single_placeholder(self):
        """Single placeholder should be replaced with protection token."""
        content = "key1=Hello {0}"

        protected_content, placeholder_map = protect_placeholders_in_properties(content)

        assert "{0}" not in protected_content
        assert "key1=Hello __PH_" in protected_content
        assert len(placeholder_map) == 1
        assert "{0}" in placeholder_map.values()

    def test_protect_multiple_placeholders_same_line(self):
        """Multiple placeholders on same line should all be protected."""
        content = "key1=Welcome {0} and {1}"

        protected_content, placeholder_map = protect_placeholders_in_properties(content)

        assert "{0}" not in protected_content
        assert "{1}" not in protected_content
        assert protected_content.count("__PH_") == 2
        assert len(placeholder_map) == 2

    def test_protect_placeholders_across_multiple_keys(self):
        """Placeholders in multiple keys should all be protected."""
        content = """key1=Hello {0}
key2=Score {0} below {1} for {2}
key3=No placeholders"""

        protected_content, placeholder_map = protect_placeholders_in_properties(content)

        assert "{0}" not in protected_content
        assert "{1}" not in protected_content
        assert "{2}" not in protected_content
        assert protected_content.count("__PH_") == 4  # 1 + 3 + 0
        assert "No placeholders" in protected_content  # Unchanged

    def test_protect_escaped_single_quotes_with_placeholders(self):
        """Escaped single quotes around placeholders should be preserved."""
        content = "key1=Address ''{0}'' in transaction ''{1}''"

        protected_content, placeholder_map = protect_placeholders_in_properties(content)

        assert "''" in protected_content  # Escaped quotes preserved
        assert "{0}" not in protected_content
        assert "{1}" not in protected_content
        assert protected_content.count("__PH_") == 2

    def test_restore_protected_placeholders(self):
        """Protected placeholders should be restored to original values."""
        original = "key1=Hello {0} and {1}"

        protected, placeholder_map = protect_placeholders_in_properties(original)
        restored = restore_placeholders_in_properties(protected, placeholder_map)

        assert restored == original
        assert "{0}" in restored
        assert "{1}" in restored
        assert "__PH_" not in restored

    def test_restore_maintains_escaped_quotes(self):
        """Restoration should maintain escaped single quotes."""
        original = "key1=Address ''{0}'' in ''{1}''"

        protected, placeholder_map = protect_placeholders_in_properties(original)
        restored = restore_placeholders_in_properties(protected, placeholder_map)

        assert restored == original
        assert "''{0}''" in restored
        assert "''{1}''" in restored

    def test_protect_multiline_properties(self):
        """Multiline property values should have all placeholders protected."""
        content = """key1=Your score {0} is below {1}\\n\\
for minimum range {2}.\\n\\
Bisq Easy''s model."""

        protected, placeholder_map = protect_placeholders_in_properties(content)

        assert "{0}" not in protected
        assert "{1}" not in protected
        assert "{2}" not in protected
        assert "Bisq Easy''s model" in protected  # Text preserved
        assert len(placeholder_map) == 3

    def test_empty_content(self):
        """Empty content should return empty results."""
        content = ""

        protected, placeholder_map = protect_placeholders_in_properties(content)

        assert protected == ""
        assert len(placeholder_map) == 0

    def test_no_placeholders(self):
        """Content without placeholders should pass through unchanged."""
        content = """key1=Hello world
key2=No placeholders here"""

        protected, placeholder_map = protect_placeholders_in_properties(content)

        assert protected == content
        assert len(placeholder_map) == 0

    def test_json_object_syntax_is_not_treated_as_placeholder(self):
        """JSON review snippets should keep structural braces visible."""
        content = json.dumps({"/hello": "Hello"})

        protected, placeholder_map = protect_placeholders_in_properties(content)

        assert protected == content
        assert placeholder_map == {}

    def test_unique_protection_tokens(self):
        """Each placeholder should get a unique protection token."""
        content = "key1={0} and {0}"  # Same placeholder repeated

        protected, placeholder_map = protect_placeholders_in_properties(content)

        # Each occurrence should get its own unique token
        tokens = [k for k in placeholder_map.keys()]
        assert len(tokens) == len(set(tokens))  # All unique

        # But they should all map to the same value
        values = list(placeholder_map.values())
        assert all(v == "{0}" for v in values)

    def test_restore_with_ai_modifications(self):
        """Restoration should work even if AI modified surrounding text."""
        original = "key1=Your score {0} is below {1}"

        protected, placeholder_map = protect_placeholders_in_properties(original)

        # Simulate AI modifying the text but keeping tokens
        token1, token2 = list(placeholder_map.keys())
        ai_modified = f"key1=Score {token1} below {token2} for range"

        restored = restore_placeholders_in_properties(ai_modified, placeholder_map)

        assert "{0}" in restored
        assert "{1}" in restored
        assert "__PH_" not in restored
        assert "Score {0} below {1} for range" in restored

    def test_protection_preserves_file_structure(self):
        """Protection should maintain comments and blank lines."""
        content = """# Comment
key1=Hello {0}

key2=World {1}"""

        protected, placeholder_map = protect_placeholders_in_properties(content)

        assert "# Comment" in protected
        assert "\n\n" in protected  # Blank line preserved
        assert "__PH_" in protected


class _NullAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_holistic_review_uses_compatible_completion_token_limit():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps({"key1": "Hallo {0}"})
                )
            )
        ]
    )
    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(return_value=response)
    provider.is_retryable_error.return_value = False

    with (
        patch("src.translate_localization_files.DRY_RUN", False),
        patch("src.translate_localization_files.REVIEW_MODEL_NAME", "gpt-5.4-mini"),
        patch("src.translate_localization_files.MODEL_PROVIDER", provider),
    ):
        result = await holistic_review_async(
            source_content="key1=Hello {0}",
            translated_content="key1=Hallo {0}",
            target_language="German",
            keys_to_review=["key1"],
            semaphore=asyncio.Semaphore(1),
            rate_limiter=_NullAsyncContext(),
            style_rules_text="",
        )

    assert result == {"key1": "Hallo {0}"}
    kwargs = provider.create_chat_completion.await_args.kwargs
    assert kwargs["model"] == "gpt-5.4-mini"
    assert kwargs["completion_token_limit"] == 8192
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs
