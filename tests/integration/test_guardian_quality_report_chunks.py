"""Internal report chunks stay bounded without becoming public payloads."""

import pytest

from localize.guardian import quality_report_publication as publication
from localize.guardian import quality_reports
from localize.semantic_quality import TranslationChange
from tests.integration.test_guardian_quality_publication import pull_payload
from tests.unit.test_guardian_controller import TARGET_PATH, _pull


def changes_for_keys(keys):
    return [TranslationChange(TARGET_PATH, "ru", key, "Hello reader", None, "Hello reader") for key in keys]


def test_multibyte_keys_split_before_wire_limit_without_losing_findings():
    keys = [f"key{index:03}-" + "ключ" * 200 for index in range(80)]
    reports = publication.reports_from_changes(_pull(), changes_for_keys(keys))
    assert len(reports) > 1
    assert [item["key"] for report in reports for item in report["findings"]] == keys
    for report in reports:
        body = quality_reports.render_report(report)
        assert len(body.encode("utf-8")) <= quality_reports.MAX_REPORT_BYTES
        assert len(report["findings"]) <= quality_reports.MAX_FINDINGS
        assert quality_reports.parse_report(body) == report


@pytest.mark.parametrize("margin,expected_chunks", [(0, 1), (-1, 2)])
@pytest.mark.parametrize("suffix", ["文" * 100, '\\"\t' * 100])
def test_exact_encoded_byte_boundary(monkeypatch, margin, expected_chunks, suffix):
    keys = ["first" + "я" * 100, "second" + suffix]
    baseline = publication.reports_from_changes(_pull(), changes_for_keys(keys))[0]
    exact_bytes = len(quality_reports.render_report(baseline).encode("utf-8"))
    monkeypatch.setattr(quality_reports, "MAX_REPORT_BYTES", exact_bytes + margin)
    reports = publication.reports_from_changes(_pull(), changes_for_keys(keys))
    assert len(reports) == expected_chunks
    assert sum(len(report["findings"]) for report in reports) == 2


def test_byte_chunks_still_respect_finding_count_limit():
    reports = publication.reports_from_changes(_pull(), changes_for_keys([f"key{index}" for index in range(101)]))
    assert [len(report["findings"]) for report in reports] == [100, 1]


def test_single_finding_that_cannot_fit_fails_clearly(monkeypatch):
    monkeypatch.setattr(quality_reports, "MAX_REPORT_BYTES", 500)
    with pytest.raises(ValueError, match="single machine finding.*fit"):
        publication.reports_from_changes(_pull(), changes_for_keys(["ключ" * 200]))


def test_size_driven_chunks_cannot_exceed_total_report_limit(monkeypatch):
    monkeypatch.setattr(publication, "MAX_REPORTS", 1)
    with pytest.raises(ValueError, match="chunks exceed"):
        publication.reports_from_changes(_pull(), changes_for_keys([f"key{index}-" + "ключ" * 200 for index in range(80)]))


def test_size_chunked_publication_replay_preserves_truthful_accounting():
    pull = _pull()
    changes = changes_for_keys([f"key{index}-" + "ключ" * 200 for index in range(80)])
    reports = publication.reports_from_changes(pull, changes)
    actor = {"id": 8, "type": "User"}
    comments = []

    def request(method, endpoint, data=None):
        if endpoint == "/user":
            return actor
        if method == "PAGINATE":
            return comments
        if method == "POST":
            comment = {"body": data["body"], "user": actor}
            comments.append(comment)
            return comment
        return pull_payload(pull)

    kwargs = dict(repository=pull.repository, pull_number=pull.number, expected_head=pull.head_sha, request=request)
    first = publication.publish_reports(reports=reports, **kwargs)
    assert first == {"finding_count": 80, "report_count": len(reports),
                     "published": 1, "already_present": 0}
    second = publication.publish_reports(reports=publication.reports_from_changes(pull, changes), **kwargs)
    assert second == {"finding_count": 80, "report_count": len(reports),
                      "published": 0, "already_present": 1}
    assert len(comments) == 1
    assert "80 source-identical values" in comments[0]["body"]
    assert len(comments[0]["body"].encode("utf-8")) < 2000
