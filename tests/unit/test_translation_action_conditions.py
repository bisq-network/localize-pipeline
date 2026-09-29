import json

import pytest

from localize.semantic_quality import TranslationChange
from localize.translation_semantic_reviewer import (
    build_semantic_review_messages,
    normalize_review_response,
)


@pytest.mark.parametrize(
    ("locale", "language", "bad_target", "good_target"),
    [
        (
            "tr",
            "Turkish",
            "Teklif oluşturulduğunda fiyat sapması eşiği aşarsa uyarı gösterilir.",
            "Fiyat sapması eşiği aşan bir teklifi kabul ettiğinizde uyarı gösterilir.",
        ),
        (
            "vi",
            "Vietnamese",
            "Cảnh báo xuất hiện khi độ lệch giá vượt ngưỡng.",
            "Cảnh báo xuất hiện khi bạn chấp nhận một đề nghị có độ lệch giá vượt ngưỡng.",
        ),
    ],
)
def test_semantic_review_checks_offer_acceptance_as_well_as_price_threshold(
    locale, language, bad_target, good_target
):
    source = (
        "A warning appears when you accept an offer whose price deviation "
        "exceeds the threshold."
    )
    changes = [
        TranslationChange(
            file=f"messages_{locale}.properties",
            locale_code=locale,
            key=f"help.{variant}",
            source_value=source,
            old_value=None,
            new_value=target,
        )
        for variant, target in (("bad", bad_target), ("good", good_target))
    ]

    messages = build_semantic_review_messages(language, changes, [], [])
    instruction = messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert [item["source_value"] for item in payload["changes"]] == [source, source]
    assert [item["new_target_value"] for item in payload["changes"]] == [
        bad_target,
        good_target,
    ]
    assert "what event triggers it" in instruction
    assert "omits or changes an action trigger" in instruction
    assert "even when it preserves a numeric threshold" in instruction
    assert "Do not flag a paraphrase that preserves both" in instruction

    # A reviewer finding for the changed trigger must remain scoped to the
    # faulty entry; the version retaining both conditions stays clean.
    response = json.dumps(
        {
            "findings": [
                {
                    "file": changes[0].file,
                    "key": changes[0].key,
                    "severity": "error",
                    "reason": "The offer-acceptance trigger is missing or changed.",
                }
            ]
        }
    )
    findings = normalize_review_response(response, changes)
    assert [(finding["key"], finding["severity"]) for finding in findings] == [
        ("help.bad", "error")
    ]
