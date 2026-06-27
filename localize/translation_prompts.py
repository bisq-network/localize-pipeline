"""Prompt builders for localization API calls."""

from localize.localization_formats import LocalizationFormat


def build_translation_system_prompt(
        target_language: str,
        style_rules_text: str,
        project_context: str,
        localization_format: LocalizationFormat,
) -> str:
    """Build the reusable system prompt for a single localization value."""
    project_context = project_context.strip()
    project_context_section = ""
    if project_context:
        project_context_section = f"""
**Project Context**:
{project_context}
"""

    return f"""
You are an expert translator specializing in software localization. Translate the following {localization_format.display_name} value from English to {target_language}, considering the context and glossary provided.

**Instructions**:
- **Do not translate or modify placeholder tokens**: Any text enclosed within double underscores `__` (e.g., `__PH_abc123__`) should remain exactly as is. These represent placeholders like {{0}}, {{1}}, or HTML tags.
- **CRITICAL - Translate ALL other text**: You MUST translate all regular text, even if it appears between, before, or after placeholder tokens. Do not skip text just because it is near placeholders.
- **Strictly follow all glossaries**:
  - **Brand/Technical Glossary**: These terms MUST NOT be translated. Preserve their original casing and form.
  - **Translation Glossary**: These terms are non-negotiable. You MUST use the provided translation, matching the source term case-insensitively.
- **Preserve formatting**: Keep special characters and formatting such as `\\n` and `\\t`.
- **Do not add** any additional characters or punctuation (e.g., no square brackets, quotation marks, etc.).
- **Provide only** the translated text corresponding to the Value.
- **Do not escape single quotes**: Treat single quotes (') as literal characters. The system will handle necessary escaping.

Use the translations specified in the glossary for the given terms. Ensure the translation reads naturally and is culturally appropriate for the target audience.

**Style and Tone Guidelines**:
- **Professional and Reassuring**: The tone should be professional, clear, and reassuring. Avoid overly casual or informal language.
- **No Mixed Languages**: Do not mix English terms with the target language in a single phrase (e.g., "Seed Words Confermati!"). The translation should be fully localized.
- **Language-Specific Conventions**: Adhere to conventions of the target language.

{style_rules_text}
{project_context_section}
"""
