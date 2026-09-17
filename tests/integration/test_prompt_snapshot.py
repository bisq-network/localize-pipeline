import json
import os
from pathlib import Path

import pytest
from aiolimiter import AsyncLimiter

import localize.translate_localization_files as translation_runtime
from localize.localization_adapters import get_localization_adapter
from localize.localization_formats import JAVA_PROPERTIES_FORMAT
from localize.placeholder_rules import capture_placeholder_tokens, protect_placeholders
from localize.prompt_capture import PromptCaptureProvider
from localize.properties_parser import parse_properties_file


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "bisq_prompt_snapshot"
SOURCE_FILE = FIXTURE_ROOT / "messages.properties"
TARGET_FILE = FIXTURE_ROOT / "messages_de.properties"
SNAPSHOT_FILE = FIXTURE_ROOT / "prompt-requests.json"


@pytest.mark.asyncio
async def test_bisq_draft_and_review_prompt_snapshot(monkeypatch):
    """Pin actual draft and review requests without live inference."""
    _, source_translations = parse_properties_file(str(SOURCE_FILE))
    _, target_translations = parse_properties_file(str(TARGET_FILE))
    keys = list(source_translations)
    assert len(keys) == 12

    monkeypatch.setattr(translation_runtime, "LANGUAGE_CODES", {"de": "German"})
    monkeypatch.setattr(translation_runtime, "NAME_TO_CODE", {"german": "de"})
    monkeypatch.setattr(translation_runtime, "MODEL_NAME", "capture-model")
    monkeypatch.setattr(translation_runtime, "REVIEW_MODEL_NAME", "capture-review-model")
    monkeypatch.setattr(translation_runtime, "REVIEW_REASONING_EFFORT", None)
    monkeypatch.setattr(translation_runtime, "PROJECT_CONTEXT", "Bisq desktop trading application.")
    monkeypatch.setattr(translation_runtime, "BRAND_GLOSSARY", ["Bisq", "BTC", "Tor"])
    monkeypatch.setattr(
        translation_runtime,
        "PRECOMPUTED_STYLE_RULES_TEXT",
        {"de": "Use concise German desktop UI terminology."},
    )

    with capture_placeholder_tokens():
        snapshot_key = "offer.repeated"
        draft_responses = [protect_placeholders(target_translations[snapshot_key])[0]]
        draft_provider = PromptCaptureProvider(stage="draft", responses=draft_responses)
        monkeypatch.setattr(translation_runtime, "MODEL_PROVIDER", draft_provider)

        result = await translation_runtime.translate_text_async(
            text=source_translations[snapshot_key],
            key=snapshot_key,
            existing_translations=target_translations,
            source_translations=source_translations,
            target_language="German",
            glossary={"de": {"trade": "Handel", "security deposit": "Sicherheitsleistung"}},
            semaphore=translation_runtime.asyncio.Semaphore(1),
            rate_limiter=AsyncLimiter(1000, 1),
            index=0,
            localization_format=JAVA_PROPERTIES_FORMAT,
        )
        assert result[2] is True

        adapter = get_localization_adapter(JAVA_PROPERTIES_FORMAT)
        review_provider = PromptCaptureProvider(stage="review", responses=["{}"])
        monkeypatch.setattr(translation_runtime, "MODEL_PROVIDER", review_provider)
        review_result = await translation_runtime.holistic_review_async(
            source_content=adapter.build_review_content(source_translations, keys),
            translated_content=adapter.build_review_content(target_translations, keys),
            target_language="German",
            keys_to_review=keys,
            semaphore=translation_runtime.asyncio.Semaphore(1),
            rate_limiter=AsyncLimiter(1000, 1),
            style_rules_text="Use concise German desktop UI terminology.",
            localization_format=JAVA_PROPERTIES_FORMAT,
        )
        assert review_result == {}

    captured = {
        "draft": draft_provider.canonical_payloads(),
        "review": review_provider.canonical_payloads(),
    }
    if os.environ.get("LOCALIZE_UPDATE_PROMPT_SNAPSHOT") == "1":
        SNAPSHOT_FILE.write_text(
            json.dumps(captured, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    assert captured == json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
