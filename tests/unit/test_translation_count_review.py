"""Count templates must reach semantic review in their visible forms."""

import json

from localize.semantic_quality import TranslationChange
from localize.translation_semantic_reviewer import build_semantic_review_messages


def _review(key: str, source: str, target: str):
    change = TranslationChange(
        file="messages_es.properties",
        locale_code="es",
        key=key,
        source_value=source,
        old_value=None,
        new_value=target,
    )
    messages = build_semantic_review_messages("Spanish", [change], [], [])
    return messages[0]["content"], json.loads(messages[1]["content"])["changes"][0]


def test_count_variants_expose_bad_and_correct_one_two_renders():
    cases = (
        ("usage.1", "Used {0} time", "Usado {0} veces", "Usado {0} vez", 1),
        ("usage.*", "Used {0} times", "Usado {0} vez", "Usado {0} veces", 2),
    )
    for key, source, defective, corrected, count in cases:
        bad_instruction, bad = _review(key, source, defective)
        good_instruction, good = _review(key, source, corrected)

        assert "Report incorrect count grammar as an error" in bad_instruction
        assert good_instruction == bad_instruction
        assert bad.get("count_renders") == [{
            "count": count,
            "source": source.replace("{0}", str(count)),
            "target": defective.replace("{0}", str(count)),
        }]
        assert good.get("count_renders") == [{
            "count": count,
            "source": source.replace("{0}", str(count)),
            "target": corrected.replace("{0}", str(count)),
        }]


def test_unsuffixed_count_and_shared_target_forms_are_reviewed_without_false_rule():
    instruction, change = _review(
        "usage.accessibility", "Used {{0}} time(s)", "Utilisé {{0}} fois"
    )

    assert change.get("count_renders") == [
        {"count": 1, "source": "Used 1 time(s)", "target": "Utilisé 1 fois"},
        {"count": 2, "source": "Used 2 time(s)", "target": "Utilisé 2 fois"},
    ]
    assert "Accept identical forms when" in instruction
    _, ordinary = _review("product.name", "Bisq", "Bisq")
    assert "count_renders" not in ordinary
