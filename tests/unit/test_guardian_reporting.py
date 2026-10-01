"""Public reporting never serializes private model prose."""

import pytest

from localize.guardian.reporting import report_body, report_disposition


def test_alternative_keeps_glossary_decision_open():
    details = dict(
        outcome="applied",
        report_reason="alternative_glossary",
        decision_required=True,
        commit_sha="a" * 40,
    )
    assert report_disposition(details) == "applied_alternative"
    body = report_body(details, repository="acme/app", feedback_id="review_comment:12")
    assert "configured glossary" in body
    assert "maintainer decision" in body
    assert "a" * 40 in body
    assert "resolved" not in body


def test_no_edit_human_report_never_claims_correction_or_copies_private_reason():
    details = dict(
        outcome="needs_human",
        report_reason="glossary_conflict",
        decision_required=True,
        commit_sha=None,
        rationale="/Users/private/token sk-secret password=secret",
    )
    body = report_body(details, repository="acme/app", feedback_id="review_comment:12")
    assert "No correction was published" in body
    assert "glossary" in body
    assert "/Users/" not in body and "secret" not in body


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("translation_batch_deferred", "deferred"),
        ("failed", "deferred"),
        ("already_addressed", "already_addressed"),
        ("not_applicable", "not_applicable"),
        ("needs_human", "needs_human"),
    ],
)
def test_explicit_dispositions(outcome, expected):
    assert report_disposition({"outcome": outcome}) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("report_reason", "/Users/private"),
        ("commit_sha", "sk-secret"),
    ],
)
def test_public_metadata_is_allowlisted(field, value):
    with pytest.raises(ValueError):
        report_body(
            {field: value}, repository="acme/app", feedback_id="review_comment:12"
        )


@pytest.mark.parametrize(
    "feedback_id", ["review_comment:/Users/private", "issue_comment:12@evil"]
)
def test_untrusted_feedback_identifiers_cannot_escape_into_public_text(feedback_id):
    with pytest.raises(ValueError):
        report_body({}, repository="acme/app", feedback_id=feedback_id)


CLEAN_CODERABBIT_SUMMARY = """<!-- This is an auto-generated comment: summarize by coderabbit.ai -->
<!-- recent_review_start -->

No actionable comments were generated in the recent review. 🎉

<details><summary>ℹ️ Recent review info</summary>
<details><summary>📒 Files selected for processing (1)</summary>
* `l10n/messages_ru.properties`
</details>
</details>
<!-- recent_review_end -->
<!-- walkthrough_start -->
The translation now follows the glossary.
<!-- walkthrough_end -->
"""


def _clean_review_event(**overrides):
    """Build an admitted CodeRabbit summary with controllable feedback fields."""
    from localize.guardian.models import FeedbackEvent

    values = dict(
        repository="acme/app", pr_number=12, kind="issue_comment", event_id="44",
        author="coderabbitai[bot]", author_id=999, author_type="Bot",
        body=CLEAN_CODERABBIT_SUMMARY, head_sha="a" * 40, base_sha="b" * 40,
        locale="ru",
    )
    return FeedbackEvent(**(values | overrides))


def _clean_review_details(**overrides):
    """Build a completed no-op assessment with optional reporting variations."""
    return dict(
        outcome="not_applicable", verdict="reject", report_reason="not_applicable",
        decision_required=False, changed_keys=0, commit_sha=None,
        recurrence_candidates=0,
    ) | overrides


@pytest.mark.parametrize("prevention", [{}, {"recurrence_candidates": 1, "prevention_pending": True}])
def test_clean_coderabbit_review_is_quiet_only_after_noop_assessment(prevention):
    """Keep clean reviews private while independent prevention work continues."""
    from localize.guardian.reporting import quiet_clean_review

    assert quiet_clean_review(_clean_review_event(), _clean_review_details(**prevention))


@pytest.mark.parametrize("changes", [
    {"verdict": "needs_human"}, {"decision_required": True},
    {"report_reason": "glossary_conflict"}, {"outcome": "already_addressed"},
    {"report_outcome": "failed"}, {"changed_keys": 1}, {"commit_sha": "a" * 40},
    {"held_value_edits": 1}, {"deferred_value_edits": 1},
])
def test_clean_summary_cannot_silence_actionable_assessment(changes):
    """Require public reporting for decisions, corrections, and incomplete work."""
    from localize.guardian.reporting import quiet_clean_review

    assert not quiet_clean_review(_clean_review_event(), _clean_review_details(**changes))


@pytest.mark.parametrize("changes", [
    {"author": "reviewer"}, {"author_type": "User"}, {"kind": "review_comment"},
    {"body": "No actionable comments were generated in the recent review."},
    {"body": CLEAN_CODERABBIT_SUMMARY.replace("recent_review_end", "missing_end")},
    {"body": CLEAN_CODERABBIT_SUMMARY + "<!-- recent_review_start -->"},
    {"body": CLEAN_CODERABBIT_SUMMARY + "<summary>🧹 Nitpick comments (1)</summary>Fix the wording."},
    {"body": CLEAN_CODERABBIT_SUMMARY + "<summary>⚠️ Outside diff range comments (1)</summary>Fix the wording."},
    {"body": CLEAN_CODERABBIT_SUMMARY + "**Actionable comments posted: 1**"},
    {"body": CLEAN_CODERABBIT_SUMMARY + "```suggestion\nFix the wording.\n```"},
    {"body": CLEAN_CODERABBIT_SUMMARY + "Please replace word X with word Y."},
    {"body": CLEAN_CODERABBIT_SUMMARY.replace("<!-- walkthrough_start -->", "<!-- walkthrough_start -->\n<summary>🧹 Nitpick comments (1)</summary>")},
    {"body": CLEAN_CODERABBIT_SUMMARY.replace("<!-- walkthrough_start -->", "<!-- walkthrough_start -->\nOutside diff range comments: 1")},
])
def test_mixed_or_unrecognized_review_still_gets_public_accountability(changes):
    """Preserve reporting for real findings and unsupported summary formats."""
    from localize.guardian.reporting import quiet_clean_review

    assert not quiet_clean_review(_clean_review_event(**changes), _clean_review_details())
