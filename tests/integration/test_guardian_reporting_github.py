"""Exercise reporting through the real HTTP broker, including ambiguous writes."""

import json
import subprocess

import httpx
import pytest

from localize.guardian.github import (
    GitHubAPIError,
    GitHubAuthenticationError,
    PolicyViolation,
)
from tests.unit.test_guardian_github import (
    BASE_SHA,
    HEAD_SHA,
    EXPECTED_ACTOR,
    _json_response,
    _policy,
    _pr_payload,
    _repo_payload,
    _user_payload,
    _write_broker,
)


@pytest.fixture
def boundary(monkeypatch):
    state = {
        "comments": [],
        "writes": [],
        "lost_ack": False,
        "pull": _pr_payload(1),
        "actor": _user_payload(),
        "patch_error": None,
        "parent_pull": "https://api.github.test/repos/acme/app/pulls/1",
    }

    def handler(request):
        path, method = request.url.path, request.method
        if path == "/user":
            return _json_response(request, state["actor"])
        if path == "/repos/acme/app":
            return _json_response(request, _repo_payload())
        if path == "/repos/acme/app/pulls/1":
            return _json_response(request, state["pull"])
        if path == "/repos/acme/app/pulls/comments/12":
            return _json_response(
                request, {"id": 12, "pull_request_url": state["parent_pull"]}
            )
        collections = {
            "/repos/acme/app/pulls/1/comments",
            "/repos/acme/app/issues/1/comments",
        }
        if path in collections and method == "GET":
            review = "/pulls/" in path
            return _json_response(
                request,
                [
                    c
                    for c in state["comments"]
                    if bool(c.get("in_reply_to_id")) == review
                ],
            )
        if path.startswith("/repos/acme/app/issues/comments/"):
            comment = next(
                c for c in state["comments"] if c["id"] == int(path.rsplit("/", 1)[1])
            )
            if method == "GET":
                return _json_response(request, dict(comment))
            assert method == "PATCH"
            if state["patch_error"] == "before":
                raise httpx.ReadTimeout("request lost before PATCH", request=request)
            comment["body"] = json.loads(request.content)["body"]
            state["writes"].append((method, path))
            if state["patch_error"] == "after":
                raise httpx.ReadTimeout("response lost after PATCH", request=request)
            return _json_response(request, dict(comment))
        if method == "POST" and path in {
            "/repos/acme/app/pulls/1/comments/12/replies",
            "/repos/acme/app/issues/1/comments",
        }:
            number = 100 + len(state["comments"])
            review = path.endswith("/replies")
            comment = {
                "id": number,
                "user": _user_payload(),
                "body": json.loads(request.content)["body"],
                "html_url": f"https://github.test/acme/app/pull/1#{'discussion_r' if review else 'issuecomment-'}{number}",
            }
            if review:
                comment["in_reply_to_id"] = 12
            state["comments"].append(comment)
            state["writes"].append((method, path))
            if state["lost_ack"]:
                state["lost_ack"] = False
                raise httpx.ReadTimeout("lost response", request=request)
            return _json_response(request, dict(comment), status=201)
        raise AssertionError(f"Unexpected write or endpoint: {method} {path}")

    monkeypatch.setattr(
        "localize.guardian.credentials.run_bounded_process",
        lambda *a, **k: subprocess.CompletedProcess(
            ["test-helper"], 0, stdout="test-token", stderr=""
        ),
    )
    broker = _write_broker(
        policy=_policy(),
        token_command=("test-helper",),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    return broker, state


def arguments():
    return dict(
        pull_number=1,
        expected_head_sha=HEAD_SHA,
        expected_base_sha=BASE_SHA,
        expected_actor=EXPECTED_ACTOR,
        before_create=lambda: None,
    )


def report_arguments():
    return dict(
        **arguments(),
        report_id="f" * 64,
        feedback_id="review_comment:12",
        details={
            "outcome": "needs_human",
            "report_reason": "glossary_conflict",
            "decision_required": True,
        },
    )


def test_thread_reply_recovers_lost_response_without_duplicate(boundary):
    broker, state = boundary
    state["lost_ack"] = True
    with pytest.raises(GitHubAPIError):
        broker.post_feedback_report(**report_arguments())
    result = broker.post_feedback_report(**report_arguments())
    assert not result.created
    assert len(state["writes"]) == 1
    assert "glossary" in result.body and "maintainer decision" in result.body
    assert "#discussion_r12" in result.body


@pytest.mark.parametrize("change", ["closed", "head", "base", "actor", "thread"])
def test_report_revalidates_all_write_authority(boundary, change):
    broker, state = boundary
    if change == "closed":
        state["pull"]["state"] = "closed"
    elif change in {"head", "base"}:
        state["pull"][change]["sha"] = "f" * 40
    elif change == "actor":
        state["actor"] = _user_payload(actor_id=999)
    else:
        state["parent_pull"] = "https://api.github.test/repos/acme/app/pulls/2"
    with pytest.raises((PolicyViolation, GitHubAuthenticationError)):
        broker.post_feedback_report(**report_arguments())
    assert not state["writes"]


def test_edited_feedback_callback_stops_post_after_remote_reads(boundary):
    broker, state = boundary
    calls = []

    def changed():
        calls.append(True)
        if len(calls) == 2:
            raise PolicyViolation("feedback was deleted or edited")

    with pytest.raises(PolicyViolation):
        broker.post_feedback_report(**{**report_arguments(), "before_create": changed})
    assert not state["writes"]


def test_summary_is_single_managed_comment_and_preserves_external_edits(boundary):
    broker, state = boundary
    report = broker.post_feedback_report(**report_arguments())
    reports = [
        {
            "url": report.html_url,
            "disposition": "needs_human",
            "decision_required": True,
        }
    ]
    first = broker.post_feedback_summary(
        **arguments(), reports=reports, previous_body=None
    )
    again = broker.post_feedback_summary(
        **arguments(), reports=reports, previous_body=None
    )
    assert not again.created
    reports[0]["disposition"] = "applied_alternative"
    updated = broker.post_feedback_summary(
        **arguments(), reports=reports, previous_body=first.body
    )
    assert updated.comment_id == first.comment_id
    assert "maintainer decision still required" in updated.body
    state["comments"][-1]["body"] += "\nHuman annotation"
    reports[0]["disposition"] = "needs_human"
    with pytest.raises(PolicyViolation):
        broker.post_feedback_summary(
            **arguments(), reports=reports, previous_body=updated.body
        )
    assert state["comments"][-1]["body"].endswith("Human annotation")
    assert len(state["comments"]) == 2


def test_review_summary_uses_linked_issue_comment(boundary):
    broker, state = boundary
    result = broker.post_feedback_report(
        **{**report_arguments(), "feedback_id": "review:12"}
    )
    assert "#pullrequestreview-12" in result.body
    assert state["writes"] == [("POST", "/repos/acme/app/issues/1/comments")]


@pytest.mark.parametrize("failure", ["before", "after"])
def test_summary_retries_known_exact_versions_after_ambiguous_patch(boundary, failure):
    from localize.guardian.reporting import summary_body

    broker, state = boundary
    reports = [
        {
            "url": "https://github.test/acme/app/pull/1#discussion_r100",
            "disposition": "needs_human",
            "decision_required": True,
        }
    ]
    first = broker.post_feedback_summary(
        **arguments(), reports=reports, previous_body=None
    )
    reports[0]["disposition"] = "applied_alternative"
    candidate = summary_body(
        reports, repository="acme/app", pull_number=1, web_base_url=broker.web_base_url
    )
    state["patch_error"] = failure
    with pytest.raises(GitHubAPIError):
        broker.post_feedback_summary(
            **arguments(), reports=reports, previous_body=(first.body, candidate)
        )
    state["patch_error"] = None
    result = broker.post_feedback_summary(
        **arguments(), reports=reports, previous_body=(first.body, candidate)
    )
    assert result.body == candidate and result.comment_id == first.comment_id
    assert len(state["comments"]) == 1
