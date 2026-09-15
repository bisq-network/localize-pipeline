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
