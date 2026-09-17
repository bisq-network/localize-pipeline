"""Draft proposals expose useful review evidence, not private machine state."""

from localize.guardian.prevention_runtime import PreventionGitHubBroker
from localize.guardian.remediation import RemediationGitHubBroker, _draft_text
from tests.unit.test_guardian_prevention import _plan
from tests.unit.test_guardian_remediation import _base_snapshot, _policy, _source_pulls, _patch


def test_prevention_plan_retains_attestation_but_renders_human_evidence(tmp_path):
    plan = _plan(tmp_path)
    assert plan.evidence_hash and plan.patch_hash
    for internal in (plan.evidence_hash, plan.patch_hash, "fingerprint", "review_comment:", "issue_comment:"):
        assert internal not in plan.body
    assert "2 reviewed feedback items" in plan.body
    assert "failed on the exact base" in plan.body
    assert "passed on its direct child" in plan.body
    assert "localize/rules.py" in plan.body
    assert plan.base_sha in plan.body and plan.candidate_sha in plan.body


def test_new_draft_identifiers_are_readable_labels_without_hidden_metadata():
    evidence_hash, candidate_sha = "a" * 64, "b" * 40
    for marker in (PreventionGitHubBroker._marker(evidence_hash, candidate_sha),
                   RemediationGitHubBroker.marker(evidence_hash, candidate_sha)):
        assert "Guardian" in marker
        assert "<!--" not in marker
        assert evidence_hash not in marker
        assert candidate_sha not in marker


def test_remediation_draft_links_source_prs_without_opaque_attestations():
    _, body = _draft_text(base=_base_snapshot(), policy=_policy(),
                         source_pulls=_source_pulls(), feedback_urls=(), patch_result=_patch(),
                         evidence_hash="a" * 64, batch_hash="b" * 64)
    assert "https://github.test/acme/translations/pull/12" in body
    assert "Changed translation entries: 2" in body
    for internal in ("a" * 64, "b" * 64, "SHA-256", "<!--"):
        assert internal not in body


def test_prevention_legacy_body_can_be_recovered_but_is_not_recreated(monkeypatch):
    import httpx
    import pytest
    from tests.unit import test_guardian_prevention_runtime as fixtures

    evidence_hash = "d" * 64
    branch = "guardian/prevention-" + evidence_hash
    body = "Old proposal\n\nEvidence fingerprint: <code>private-digest</code>"
    marker = PreventionGitHubBroker._marker(evidence_hash, fixtures.CANDIDATE_SHA, legacy=True)
    pull = fixtures._pull_payload(number=17, branch=branch, body=f"{marker}\n{body}", draft=True)
    kwargs = dict(branch=branch, expected_base_sha=fixtures.BASE_SHA,
                  candidate_sha=fixtures.CANDIDATE_SHA, evidence_hash=evidence_hash,
                  title=pull["title"], body=body)
    broker = fixtures._recovery_broker(monkeypatch, branch=branch, exact_pull=pull)
    assert broker.find_draft(**kwargs).number == 17
    assert broker.open_draft(**kwargs, before_create=lambda: pytest.fail("No write permitted")).number == 17

    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/user":
            return fixtures._response(request, fixtures._authenticated_actor_payload())
        if request.url.path == "/repos/guardian/pipeline":
            return fixtures._response(request, fixtures._repo_payload())
        if request.url.path == "/repos/guardian/pipeline/pulls":
            return fixtures._response(request, [])
        pytest.fail(f"Unexpected request: {request.url}")

    broker = PreventionGitHubBroker(policy=fixtures._prevention_policy(),
                                   token_command=("credential-helper",),
                                   base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    with pytest.raises(fixtures.PreventionRuntimeError, match="cannot be republished"):
        broker.open_draft(**kwargs, before_create=lambda: pytest.fail("No write permitted"))


def test_coordinator_adds_human_source_and_candidate_links(tmp_path):
    from datetime import UTC, datetime
    from localize.guardian.models import GuardianMode
    from localize.guardian.state import GuardianState
    from tests.unit import test_guardian_prevention_runtime as fixtures

    class Broker(fixtures._FakeBroker):
        body = None

        def open_draft(self, **kwargs):
            self.body = kwargs["body"]
            return super().open_draft(**kwargs)

    with GuardianState(tmp_path / "state.sqlite3") as state:
        now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        run_id = state.start_run(repository="acme/translations", locale="ru",
                                 mode=GuardianMode.PROPOSE_PREVENTION, started_at=now)
        broker = Broker()
        coordinator = fixtures._coordinator(state=state, tmp_path=tmp_path,
                                            broker=broker, author=fixtures._FakeAuthor())
        result = coordinator.propose(
            policy=fixtures._repository_policy(), recurrence_candidates=(fixtures._candidate(),),
            evidence_revision_ids={"review_comment:42": fixtures.OPEN_SOURCE_REVISION_ID},
            run_id=run_id, observed_at=now, require_live_lease=fixtures._live_lease,
            require_current_base_unchanged=fixtures._current_base, **fixtures._open_source_kwargs(),
        )
        assert result.failures == () and len(result.drafts) == 1
        assert "https://github.com/acme/translations/pull/12" in broker.body
        assert "https://github.com/guardian/pipeline/commit/" in broker.body
        assert "review_comment:" not in broker.body and "fingerprint" not in broker.body


def test_remediation_legacy_body_is_read_only_recoverable():
    import pytest
    from tests.unit import test_guardian_remediation as fixtures

    evidence_hash = "c" * 64
    branch = "translation-updates-history-" + evidence_hash
    body = "Old proposal\nEvidence SHA-256: `private-digest`"
    marker = RemediationGitHubBroker.marker(evidence_hash, fixtures.CANDIDATE_SHA, legacy=True)
    pull = fixtures._pull(branch=branch, title="Historical corrections", body=f"{marker}\n{body}")
    existing = True

    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        if path == "/repos/acme/translations":
            return fixtures._response(request, fixtures._repo("acme/translations", 42))
        if path == "/repos/translator/translations":
            return fixtures._response(request, fixtures._repo("translator/translations", 84))
        if path == "/repos/acme/translations/pulls":
            return fixtures._response(request, [pull] if existing else [])
        if path == "/repos/acme/translations/pulls/91":
            return fixtures._response(request, pull)
        if path == "/repos/acme/translations/issues/91/events":
            return fixtures._response(request, [])
        pytest.fail(f"Unexpected request: {request.url}")

    broker = fixtures._broker(handler)
    kwargs = dict(branch=branch, expected_base_sha=fixtures.BASE_SHA,
                  candidate_sha=fixtures.CANDIDATE_SHA, evidence_hash=evidence_hash,
                  title=pull["title"], body=body)
    assert broker.find_draft(**kwargs).number == 91
    guarded = dict(before_create=lambda: pytest.fail("No write permitted"),
                   before_post=lambda: pytest.fail("No write permitted"))
    assert broker.open_draft(**kwargs, **guarded).number == 91
    existing = False
    with pytest.raises(fixtures.RemediationRuntimeError, match="cannot be republished"):
        broker.open_draft(**kwargs, **guarded)
