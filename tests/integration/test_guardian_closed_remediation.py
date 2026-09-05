"""Composition canaries for closed-history remediation publication and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any, Sequence
from urllib.parse import unquote

import httpx
import pytest

from localize.guardian.models import (
    AllowedHeadRepository,
    ClosedPrBackfillPolicy,
    ExactRepository,
    FeedbackEvent,
    GuardianMode,
    HistoricalCheckScope,
    HistoricalRemediationPolicy,
    ProposedReplacement,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.policy import PatchResult
from localize.guardian.remediation import (
    RemediationBaseSnapshot,
    RemediationCoordinator,
    RemediationGitHubBroker,
    RemediationRuntimeError,
)
from localize.guardian.state import (
    GuardianState,
    HistoricalPullReference,
    remediation_edit_hash,
)
from localize.guardian.workspace import (
    ExactRevision,
    GuardianWorkspace,
    materialize_exact_checkout,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TARGET_PATH = "l10n/messages_ru.properties"
FEEDBACK_URL = "https://github.test/acme/translations/pull/12#discussion_r100"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _create_remotes(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Guardian Integration")
    _git(source, "config", "user.email", "guardian@example.invalid")
    _git(source, "config", "commit.gpgsign", "false")
    translations = source / "l10n"
    translations.mkdir()
    (translations / "messages_en.properties").write_text(
        "first.key=First\n", encoding="utf-8"
    )
    (translations / "messages_ru.properties").write_text(
        "first.key=old one\n", encoding="utf-8"
    )
    (source / "README.md").write_text("base\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "Create exact current base")
    base_sha = _git(source, "rev-parse", "HEAD")

    base_parent = tmp_path / "acme"
    base_parent.mkdir()
    base_remote = base_parent / "translations.git"
    result = subprocess.run(
        ("git", "clone", "--bare", str(source), str(base_remote)),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    push_parent = tmp_path / "translator"
    push_parent.mkdir()
    push_remote = push_parent / "translations.git"
    result = subprocess.run(
        ("git", "clone", "--bare", str(source), str(push_remote)),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return base_remote, push_remote, base_sha


def _advance_base(remote: Path, tmp_path: Path) -> str:
    checkout = tmp_path / "advanced-base"
    result = subprocess.run(
        ("git", "clone", str(remote), str(checkout)),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    _git(checkout, "config", "user.name", "Guardian Integration")
    _git(checkout, "config", "user.email", "guardian@example.invalid")
    _git(checkout, "config", "commit.gpgsign", "false")
    (checkout / "README.md").write_text("unrelated base advance\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "Advance base without changing translations")
    _git(checkout, "push", "origin", "HEAD:main")
    return _git(checkout, "rev-parse", "HEAD")


def _signing_spy(
    arguments: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    command = tuple(arguments)
    if "commit" in command and "-S" in command:
        command = tuple(
            "--no-gpg-sign" if argument == "-S" else argument for argument in command
        )
    if "verify-commit" in command:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, **kwargs)


def _policy() -> RepositoryPolicy:
    publisher = TrustedActor("translator", 7, "User")
    return RepositoryPolicy(
        base_repo="acme/translations",
        base_repo_id=42,
        base_branch="main",
        allowed_pr_authors=(publisher,),
        allowed_head_owners=(TrustedActor("translator", 8, "Organization"),),
        allowed_head_repositories=(
            AllowedHeadRepository("translator/translations", 84),
        ),
        allowed_branch_globs=("translation-updates-history-*",),
        allowed_path_globs=("l10n/*.properties",),
        pipeline_config_path="config.yaml",
        source_locale="en",
        trusted_reviewers={"ru": (TrustedActor("reviewer", 9, "User"),)},
        trusted_bots={},
        closed_pr_backfill=ClosedPrBackfillPolicy(
            lookback_days=120,
            max_prs_per_poll=4,
            remediation=HistoricalRemediationPolicy(
                push_repository=ExactRepository("translator/translations", 84),
                push_branch_prefix="translation-updates-history-",
                publication_actor=publisher,
            ),
        ),
    )


def _replacement(*, feedback_id: str = "review_comment:100") -> ProposedReplacement:
    return ProposedReplacement(
        feedback_id=feedback_id,
        path=TARGET_PATH,
        key="first.key",
        locale="ru",
        expected_value="old one",
        proposed_value="new one",
        confidence=0.99,
        evidence=("native reviewer",),
        source_value="First",
    )


def _seed_evidence(
    state: GuardianState,
    *,
    policy_digest: str = "2" * 64,
    pr_number: int = 12,
    pull_id: int = 500,
    event_id: str = "100",
    pull_revision_digest: str = "1" * 64,
    authority_digest: str = "5" * 64,
    head_sha: str = "c" * 40,
    base_sha: str = "d" * 40,
    feedback_url: str = FEEDBACK_URL,
) -> tuple[str, HistoricalPullReference, int]:
    event = FeedbackEvent(
        repository="acme/translations",
        pr_number=pr_number,
        kind="review_comment",
        event_id=event_id,
        author="reviewer",
        author_id=9,
        author_type="User",
        body="Use the reviewed Russian wording.",
        head_sha=head_sha,
        base_sha=base_sha,
        locale="ru",
        updated_at="2026-08-30T10:00:00Z",
        path=TARGET_PATH,
        line=1,
        html_url=feedback_url,
    )
    revision = state.record_feedback_event(event, observed_at=NOW)
    source = HistoricalPullReference(
        repository="acme/translations",
        repository_id=42,
        pull_id=pull_id,
        pr_number=pr_number,
        pull_revision_digest=pull_revision_digest,
        authority_digest=authority_digest,
        policy_digest=policy_digest,
        head_sha=event.head_sha,
        base_sha=event.base_sha,
    )
    state.record_historical_pull_completion(
        repository=source.repository,
        repository_id=source.repository_id,
        pull_id=source.pull_id,
        pr_number=source.pr_number,
        pull_revision_digest=source.pull_revision_digest,
        policy_digest=source.policy_digest,
        authority_scope=HistoricalCheckScope.ASSESSMENT,
        completed_at=NOW,
        head_sha=source.head_sha,
        base_sha=source.base_sha,
        event_revision_ids=(revision.revision_id,),
    )
    run_id = state.start_run(
        repository=source.repository,
        locale="ru",
        mode=GuardianMode.PROPOSE_PREVENTION,
        started_at=NOW,
    )
    return run_id, source, revision.revision_id


@dataclass
class _LocalRemoteWorkspace:
    workspace: GuardianWorkspace
    remote: Path
    crash_after_push: bool = False

    @property
    def path(self) -> Path:
        return self.workspace.path

    @property
    def revision(self) -> ExactRevision:
        return self.workspace.revision

    @property
    def original_sha(self) -> str:
        return self.workspace.original_sha

    def commit_historical_remediation_changes(self, **kwargs: Any):
        return self.workspace.commit_historical_remediation_changes(**kwargs)

    def publish_remediation_branch(self, commit, **kwargs: Any):
        published = self.workspace.publish_remediation_branch(
            commit,
            **kwargs,
            remote_url=self.remote.as_uri(),
            allow_file_remote=True,
        )
        if self.crash_after_push:
            self.crash_after_push = False
            raise RuntimeError("simulated crash after branch push")
        return published


class _GitHubSimulation:
    def __init__(self, remote: Path, base_sha: str) -> None:
        self.remote = remote
        self.base_sha = base_sha
        self.pulls: list[dict[str, object]] = []
        self.posts = 0
        self.lose_next_post_response = False
        self.requests: list[tuple[str, str]] = []

    def _branch_sha(self, branch: str) -> str | None:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(self.remote),
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _response(request: httpx.Request, payload: object, status: int = 200):
        return httpx.Response(status, request=request, json=payload)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method != "GET" and path != "/repos/acme/translations/pulls":
            raise AssertionError(f"unexpected GitHub mutation: {request.method} {path}")
        if path == "/user":
            return self._response(
                request, {"login": "translator", "id": 7, "type": "User"}
            )
        if path == "/repos/acme/translations":
            return self._response(
                request,
                {
                    "full_name": "acme/translations",
                    "id": 42,
                    "private": False,
                    "fork": False,
                    "owner": {"id": 8, "type": "Organization"},
                },
            )
        if path == "/repos/translator/translations":
            return self._response(
                request,
                {
                    "full_name": "translator/translations",
                    "id": 84,
                    "private": False,
                    "fork": True,
                    "parent": {"id": 42},
                    "source": {"id": 42, "fork": False},
                    "owner": {"id": 8, "type": "Organization"},
                },
            )
        if path == "/repos/acme/translations/branches/main":
            return self._response(
                request, {"name": "main", "commit": {"sha": self.base_sha}}
            )
        branch_prefix = "/repos/translator/translations/branches/"
        if path.startswith(branch_prefix):
            branch = unquote(path.removeprefix(branch_prefix))
            sha = self._branch_sha(branch)
            if sha is None:
                return self._response(request, {"message": "Not Found"}, 404)
            return self._response(request, {"name": branch, "commit": {"sha": sha}})
        if path == "/repos/acme/translations/pulls" and request.method == "GET":
            head = request.url.params.get("head", "")
            branch = head.split(":", 1)[1] if ":" in head else ""
            return self._response(
                request,
                [pull for pull in self.pulls if pull["head"]["ref"] == branch],
            )
        exact_pull_prefix = "/repos/acme/translations/pulls/"
        if path.startswith(exact_pull_prefix) and request.method == "GET":
            number = int(path.removeprefix(exact_pull_prefix))
            pull = next(
                (item for item in self.pulls if item["number"] == number),
                None,
            )
            if pull is None:
                return self._response(request, {"message": "Not Found"}, 404)
            return self._response(request, pull)
        issue_events_prefix = "/repos/acme/translations/issues/"
        if path.startswith(issue_events_prefix) and path.endswith("/events"):
            number = int(path.removeprefix(issue_events_prefix).removesuffix("/events"))
            pull = next(
                (item for item in self.pulls if item["number"] == number),
                None,
            )
            if pull is None:
                return self._response(request, {"message": "Not Found"}, 404)
            names: tuple[str, ...]
            if pull["merged_at"] is not None:
                names = ("ready_for_review", "merged", "closed")
            elif pull["state"] == "closed" and pull["draft"] is False:
                names = ("ready_for_review", "closed")
            elif pull["state"] == "closed":
                names = ("closed",)
            elif pull["draft"] is False:
                names = ("ready_for_review",)
            else:
                names = ()
            return self._response(
                request,
                [
                    {"id": number * 10 + index, "event": name}
                    for index, name in enumerate(names, start=1)
                ],
            )
        if path == "/repos/acme/translations/pulls" and request.method == "POST":
            self.posts += 1
            payload = json.loads(request.content)
            branch = str(payload["head"]).split(":", 1)[1]
            candidate_sha = self._branch_sha(branch)
            assert candidate_sha is not None
            pull = {
                "id": 9_000 + self.posts,
                "number": 90 + self.posts,
                "html_url": f"https://github.test/acme/translations/pull/{90 + self.posts}",
                "state": "open",
                "closed_at": None,
                "merged_at": None,
                "draft": True,
                "maintainer_can_modify": False,
                "title": payload["title"],
                "body": payload["body"],
                "user": {"login": "translator", "id": 7, "type": "User"},
                "head": {
                    "ref": branch,
                    "sha": candidate_sha,
                    "repo": {"full_name": "translator/translations", "id": 84},
                },
                "base": {
                    "ref": "main",
                    "sha": self.base_sha,
                    "repo": {"full_name": "acme/translations", "id": 42},
                },
            }
            self.pulls.append(pull)
            if self.lose_next_post_response:
                self.lose_next_post_response = False
                raise httpx.ReadError("simulated lost POST response", request=request)
            return self._response(request, pull, 201)
        raise AssertionError(f"unexpected GitHub request: {request.method} {path}")


def _coordinator(
    state: GuardianState, api: _GitHubSimulation
) -> RemediationCoordinator:
    policy = _policy()

    def broker_factory(_policy_value: RepositoryPolicy) -> RemediationGitHubBroker:
        return RemediationGitHubBroker(
            policy=policy,
            token_command=("/usr/bin/printf", "integration-token"),
            github_host="github.test",
            base_url="https://api.github.test",
            transport=httpx.MockTransport(api),
        )

    return RemediationCoordinator(
        state=state,
        broker_factory=broker_factory,
        publish_credential_environment=lambda: {},
        signing_key=None,
        signing_environment=None,
        max_drafts=1,
    )


def _publish(
    coordinator: RemediationCoordinator,
    workspace: _LocalRemoteWorkspace,
    base_sha: str,
    run_id: str,
    source: HistoricalPullReference,
    revision_id: int,
    *,
    replacement: ProposedReplacement | None = None,
    feedback_url: str = FEEDBACK_URL,
):
    replacement = replacement or _replacement()
    (workspace.path / TARGET_PATH).write_text("first.key=new one\n", encoding="utf-8")
    return coordinator.publish(
        policy=_policy(),
        base=RemediationBaseSnapshot(
            revision=ExactRevision(
                host="github.test",
                owner="acme",
                repository="translations",
                ref="refs/heads/main",
                sha=base_sha,
            ),
            target_repository_id=42,
            push_repository_id=84,
            private=False,
        ),
        workspace=workspace,
        patch_result=PatchResult(
            changed_files=(TARGET_PATH,),
            changed_keys=((TARGET_PATH, "first.key"),),
        ),
        replacements=(replacement,),
        source_pulls=(source,),
        event_revision_ids=(revision_id,),
        feedback_urls=(feedback_url,),
        run_id=run_id,
        observed_at=NOW,
        require_live_lease=lambda: None,
        require_current_base_unchanged=lambda: None,
        require_exact_sources_still_closed=lambda _sources, _revision_ids: None,
        require_no_open_translation_overlap=lambda _paths, _excluded: None,
        prior_draft_keys_by_source={source: ()},
        required_edit_hashes_by_source={source: (remediation_edit_hash(replacement),)},
    )


def _checkout(remote: Path, base_sha: str, tmp_path: Path):
    return materialize_exact_checkout(
        ExactRevision(
            host="github.test",
            owner="acme",
            repository="translations",
            ref="refs/heads/main",
            sha=base_sha,
        ),
        remote_url=remote.as_uri(),
        allow_file_remote=True,
        temporary_root=tmp_path,
        _process_runner=_signing_spy,
    )


def test_real_state_git_and_mock_github_publish_one_current_base_draft(tmp_path: Path):
    base_remote, push_remote, base_sha = _create_remotes(tmp_path)
    api = _GitHubSimulation(push_remote, base_sha)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id, source, revision_id = _seed_evidence(state)
        coordinator = _coordinator(state, api)
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            outcome = _publish(
                coordinator,
                _LocalRemoteWorkspace(raw_workspace, push_remote),
                base_sha,
                run_id,
                source,
                revision_id,
            )

        assert len(outcome.drafts) == 1
        assert outcome.drafts[0].created is True
        assert api.posts == 1
        assert len(state.opened_remediation_drafts()) == 1
        assert state.historical_pull_is_complete(
            repository=source.repository,
            repository_id=source.repository_id,
            pull_id=source.pull_id,
            pull_revision_digest=source.pull_revision_digest,
            policy_digest=source.policy_digest,
            authority_scope=HistoricalCheckScope.REMEDIATION,
        )
        assert all(
            method == "GET"
            for method, path in api.requests
            if path.endswith("/pulls/12")
            or "/pulls/12/" in path
            or "/issues/12" in path
        )


def test_lost_post_response_survives_policy_drift_without_a_second_post(tmp_path: Path):
    base_remote, push_remote, base_sha = _create_remotes(tmp_path)
    api = _GitHubSimulation(push_remote, base_sha)
    api.lose_next_post_response = True
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id, source, revision_id = _seed_evidence(state)
        coordinator = _coordinator(state, api)
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            with pytest.raises(RemediationRuntimeError, match="request failed"):
                _publish(
                    coordinator,
                    _LocalRemoteWorkspace(raw_workspace, push_remote),
                    base_sha,
                    run_id,
                    source,
                    revision_id,
                )

        coordinator.begin_poll()
        recovered = coordinator.recover(
            policy=_policy(),
            policy_digest="9" * 64,
            observed_at=NOW,
            require_live_lease=lambda: None,
            require_current_base_unchanged=lambda: None,
            require_exact_sources_still_closed=(lambda _sources, _revision_ids: None),
            require_no_open_translation_overlap=lambda _paths, _excluded: None,
        )

        assert len(recovered.drafts) == 1
        assert recovered.drafts[0].created is False
        assert api.posts == 1
        assert state.pending_remediation_drafts() == ()


def test_closed_unmerged_draft_is_a_human_veto_for_later_identical_evidence(
    tmp_path: Path,
):
    base_remote, push_remote, base_sha = _create_remotes(tmp_path)
    api = _GitHubSimulation(push_remote, base_sha)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id, source, revision_id = _seed_evidence(state)
        coordinator = _coordinator(state, api)
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            first = _publish(
                coordinator,
                _LocalRemoteWorkspace(raw_workspace, push_remote),
                base_sha,
                run_id,
                source,
                revision_id,
            )
        assert first.drafts[0].created is True

        # A human may close the exact correction without merging it. Later review
        # evidence from a different historical PR must respect that semantic veto.
        api.pulls[0]["state"] = "closed"
        api.pulls[0]["draft"] = False
        api.pulls[0]["closed_at"] = "2026-09-02T12:01:00Z"
        api.pulls[0]["merged_at"] = None
        second_url = "https://github.test/acme/translations/pull/13#discussion_r101"
        second_run_id, second_source, second_revision_id = _seed_evidence(
            state,
            pr_number=13,
            pull_id=501,
            event_id="101",
            pull_revision_digest="3" * 64,
            head_sha="e" * 40,
            feedback_url=second_url,
        )
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            repeated = _publish(
                coordinator,
                _LocalRemoteWorkspace(raw_workspace, push_remote),
                base_sha,
                second_run_id,
                second_source,
                second_revision_id,
                replacement=_replacement(feedback_id="review_comment:101"),
                feedback_url=second_url,
            )

        assert repeated.drafts[0].created is False
        assert api.posts == 1
        assert state.historical_pull_is_complete(
            repository=second_source.repository,
            repository_id=second_source.repository_id,
            pull_id=second_source.pull_id,
            pull_revision_digest=second_source.pull_revision_digest,
            policy_digest=second_source.policy_digest,
            authority_scope=HistoricalCheckScope.REMEDIATION,
        )


def test_orphan_branch_recovery_never_posts_before_fresh_publication(tmp_path: Path):
    base_remote, push_remote, base_sha = _create_remotes(tmp_path)
    api = _GitHubSimulation(push_remote, base_sha)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id, source, revision_id = _seed_evidence(state)
        coordinator = _coordinator(state, api)
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            workspace = _LocalRemoteWorkspace(
                raw_workspace, push_remote, crash_after_push=True
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                _publish(
                    coordinator,
                    workspace,
                    base_sha,
                    run_id,
                    source,
                    revision_id,
                )

            coordinator.begin_poll()
            recovered = coordinator.recover(
                policy=_policy(),
                policy_digest=source.policy_digest,
                observed_at=NOW,
                require_live_lease=lambda: None,
                require_current_base_unchanged=lambda: None,
                require_exact_sources_still_closed=(
                    lambda _sources, _revision_ids: None
                ),
                require_no_open_translation_overlap=lambda _paths, _excluded: None,
            )
            assert recovered.deferred == 1
            assert api.posts == 0

            coordinator.begin_poll()
            published = _publish(
                coordinator,
                workspace,
                base_sha,
                run_id,
                source,
                revision_id,
            )

        assert len(published.drafts) == 1
        assert api.posts == 1


def test_base_move_publishes_new_branch_without_overwriting_orphan(tmp_path: Path):
    base_remote, push_remote, base_sha = _create_remotes(tmp_path)
    api = _GitHubSimulation(push_remote, base_sha)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id, source, revision_id = _seed_evidence(state)
        coordinator = _coordinator(state, api)
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            workspace = _LocalRemoteWorkspace(
                raw_workspace, push_remote, crash_after_push=True
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                _publish(
                    coordinator,
                    workspace,
                    base_sha,
                    run_id,
                    source,
                    revision_id,
                )
            pending = state.pending_remediation_drafts()[0]
            orphan_sha = _git(push_remote, "rev-parse", f"refs/heads/{pending.branch}")

        advanced_sha = _advance_base(base_remote, tmp_path)
        # GitHub forks share the object network. Keep the independent local bare
        # repository's base ref current so a depth-one checkout can publish the
        # new direct child under the same conditions.
        _git(
            push_remote,
            "fetch",
            "--force",
            base_remote.as_uri(),
            "refs/heads/main:refs/heads/main",
        )
        api.base_sha = advanced_sha
        coordinator.begin_poll()
        with _checkout(base_remote, advanced_sha, tmp_path) as raw_workspace:
            outcome = _publish(
                coordinator,
                _LocalRemoteWorkspace(raw_workspace, push_remote),
                advanced_sha,
                run_id,
                source,
                revision_id,
            )

        assert len(outcome.drafts) == 1
        assert outcome.drafts[0].created is True
        assert api.posts == 1
        opened = state.opened_remediation_drafts()[0]
        assert opened.branch != pending.branch
        assert (
            _git(push_remote, "rev-parse", f"refs/heads/{pending.branch}") == orphan_sha
        )


def test_superseded_orphan_uses_a_new_immutable_branch(tmp_path: Path):
    base_remote, push_remote, base_sha = _create_remotes(tmp_path)
    api = _GitHubSimulation(push_remote, base_sha)
    with GuardianState(tmp_path / "guardian.sqlite3") as state:
        run_id, source, revision_id = _seed_evidence(state)
        coordinator = _coordinator(state, api)
        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            crashing_workspace = _LocalRemoteWorkspace(
                raw_workspace,
                push_remote,
                crash_after_push=True,
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                _publish(
                    coordinator,
                    crashing_workspace,
                    base_sha,
                    run_id,
                    source,
                    revision_id,
                )

        orphan = state.pending_remediation_drafts()[0]
        orphan_sha = _git(
            push_remote,
            "rev-parse",
            f"refs/heads/{orphan.branch}",
        )
        # The same review object is edited on a newer exact pull revision. This
        # supersedes the old authority instead of inventing a second still-live
        # comment that would also need an explicit assessment classification.
        newer_url = FEEDBACK_URL
        newer_run_id, newer_source, newer_revision_id = _seed_evidence(
            state,
            pr_number=12,
            pull_id=500,
            event_id="100",
            pull_revision_digest="3" * 64,
            head_sha="e" * 40,
            feedback_url=newer_url,
        )

        coordinator.begin_poll()
        recovered = coordinator.recover(
            policy=_policy(),
            policy_digest=newer_source.policy_digest,
            observed_at=NOW,
            require_live_lease=lambda: None,
            require_current_base_unchanged=lambda: None,
            require_exact_sources_still_closed=(lambda _sources, _revision_ids: None),
            require_no_open_translation_overlap=lambda _paths, _excluded: None,
        )
        assert recovered.abandoned == 1
        assert state.pending_remediation_drafts() == ()

        coordinator.begin_poll()
        with _checkout(base_remote, base_sha, tmp_path) as raw_workspace:
            fresh = _publish(
                coordinator,
                _LocalRemoteWorkspace(raw_workspace, push_remote),
                base_sha,
                newer_run_id,
                newer_source,
                newer_revision_id,
                replacement=_replacement(feedback_id="review_comment:100"),
                feedback_url=newer_url,
            )

        assert len(fresh.drafts) == 1
        assert fresh.drafts[0].created is True
        assert api.posts == 1
        opened = state.opened_remediation_drafts()[0]
        assert opened.branch != orphan.branch
        assert (
            _git(push_remote, "rev-parse", f"refs/heads/{orphan.branch}") == orphan_sha
        )
        assert (
            _git(
                push_remote,
                "rev-parse",
                f"refs/heads/{opened.branch}",
            )
            == fresh.drafts[0].candidate_sha
        )
