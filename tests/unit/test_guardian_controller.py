"""Orchestration tests for one bounded Localize Guardian poll."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
from typing import Iterator

import pytest
import yaml

from localize.guardian.codex import (
    CodexAuthenticationError,
    CodexCapacityError,
    CodexResult,
    CodexUsage,
    GuardianFeedbackDecision,
    GuardianRecurrenceCandidate,
    GuardianReplacement,
)
from localize.guardian.controller import GuardianController
from localize.guardian.github import (
    ChangedFile,
    CodeRabbitCoverage,
    CodeRabbitCoverageStatus,
    FeedbackKind,
    FeedbackRevision,
    GitHubAuthenticationError,
    GitHubRepositoryIdentity,
    PullRequestFeedbackSnapshot,
    PullRequestSnapshot,
)
from localize.guardian.models import (
    AllowedHeadRepository,
    CodexAuthMode,
    ExactRepository,
    FeedbackEvent,
    GuardianConfig,
    GuardianLimits,
    GuardianMode,
    GuardianRuntime,
    PreventionPolicy,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.state import GuardianState
from localize.guardian.prevention_runtime import (
    PreventionBatchOutcome,
    PreventionDraftResult,
)
from localize.guardian.workspace import CommitResult, PublicationResult


UTC = timezone.utc
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
BASE_SHA = "b" * 40
HEAD_SHA = "a" * 40
COMMIT_SHA = "c" * 40
TARGET_PATH = "l10n/messages_ru.properties"


def _write_tree(root: Path, *, head_config_source_locale: str = "en") -> None:
    (root / ".localize").mkdir(parents=True)
    (root / "l10n").mkdir()
    config = {
        "target_project_root": ".",
        "input_folder": "l10n",
        "source_locale": head_config_source_locale,
        "supported_locales": [{"code": "ru", "name": "Russian"}],
        "localization_format": "java_properties",
        "localization_layout": {
            "id": "suffix",
            "base_name": "messages",
            "source_locale": head_config_source_locale,
        },
        "placeholder_profile": "java-indexed",
        "glossary_file_path": "glossary.json",
    }
    (root / ".localize/config.yaml").write_text(
        yaml.safe_dump(config),
        encoding="utf-8",
    )
    (root / ".localize/glossary.json").write_text(
        json.dumps({"ru": {}}),
        encoding="utf-8",
    )
    (root / "l10n/messages_en.properties").write_text(
        "greeting=Push to %0 was rejected (%1). %2 %3\n",
        encoding="utf-8",
    )
    (root / TARGET_PATH).write_text(
        "greeting=Старый %0 был отклонён (%1). %2 %3\n",
        encoding="utf-8",
    )


def _policy(*, repository: str = "acme/widgets") -> RepositoryPolicy:
    return RepositoryPolicy(
        base_repo=repository,
        base_repo_id=42,
        base_branch="main",
        allowed_pr_authors=(TrustedActor("translation-service", 8, "Bot"),),
        allowed_head_owners=(TrustedActor("contributor", 7, "User"),),
        allowed_head_repositories=(AllowedHeadRepository("contributor/widgets", 84),),
        allowed_branch_globs=("localize/*",),
        allowed_path_globs=("l10n/*.properties",),
        pipeline_config_path=".localize/config.yaml",
        source_locale="en",
        trusted_reviewers={
            "ru": (TrustedActor("native-reviewer", 101, "User"),),
        },
        trusted_bots={},
    )


def _prevention_policy() -> PreventionPolicy:
    return PreventionPolicy(
        target_repository=ExactRepository("guardian/pipeline", 501),
        target_base_branch="main",
        push_repository=ExactRepository("guardian/pipeline", 501),
        push_branch_prefix="guardian/prevention-",
        allowed_code_path_globs=("localize/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(("venv/bin/pytest", "tests/unit/test_rule.py", "-q"),),
        sandbox_argv_prefix=("/usr/bin/sandbox-exec", "-f", "/safe.sb"),
        max_changed_files=4,
        max_changed_bytes=16_384,
    )


def _config(
    mode: GuardianMode,
    *,
    policies: tuple[RepositoryPolicy, ...] | None = None,
    limits: GuardianLimits | None = None,
) -> GuardianConfig:
    return GuardianConfig(
        repositories=policies or (_policy(),),
        mode=mode,
        limits=limits
        or GuardianLimits(
            daily_cost_limit_usd=2,
            model_call_reservation_usd=1,
            raw_retention_days=90,
        ),
        runtime=GuardianRuntime(
            codex_auth_mode=CodexAuthMode.API_KEY,
            codex_api_key_command=("/opt/bin/model-token",),
        ),
    )


def _pull(**overrides: object) -> PullRequestSnapshot:
    values: dict[str, object] = {
        "repository": "acme/widgets",
        "base_repository_id": 42,
        "pull_id": 500,
        "number": 12,
        "state": "open",
        "html_url": "https://github.com/acme/widgets/pull/12",
        "created_at": "2026-08-30T08:00:00Z",
        "updated_at": "2026-08-30T10:00:00Z",
        "author_login": "translation-service",
        "author_id": 8,
        "author_type": "Bot",
        "head_sha": HEAD_SHA,
        "head_ref": "localize/russian",
        "head_owner": "contributor",
        "head_owner_id": 7,
        "head_owner_type": "User",
        "head_repository": "contributor/widgets",
        "head_repository_id": 84,
        "base_sha": BASE_SHA,
        "base_ref": "main",
    }
    values.update(overrides)
    return PullRequestSnapshot(**values)  # type: ignore[arg-type]


def _feedback(
    *,
    body: str = "Use the idiomatic wording.",
    updated_at: str = "2026-08-30T10:00:00Z",
) -> FeedbackRevision:
    return FeedbackRevision(
        repository="acme/widgets",
        pull_number=12,
        kind=FeedbackKind.REVIEW_COMMENT,
        source_id="44",
        node_id="node-44",
        author_login="native-reviewer",
        author_id=101,
        author_type="User",
        body=body,
        created_at="2026-08-30T09:00:00Z",
        updated_at=updated_at,
        html_url="https://github.com/acme/widgets/pull/12#discussion_r44",
        path=TARGET_PATH,
        line=1,
    )


def _snapshot(
    *,
    feedback: tuple[FeedbackRevision, ...] | None = None,
    pull: PullRequestSnapshot | None = None,
    changed_files: tuple[ChangedFile, ...] | None = None,
) -> PullRequestFeedbackSnapshot:
    return PullRequestFeedbackSnapshot(
        repository_identity=GitHubRepositoryIdentity(
            full_name="acme/widgets",
            repository_id=42,
            private=False,
        ),
        pull_request=pull or _pull(),
        feedback=feedback if feedback is not None else (_feedback(),),
        coderabbit=CodeRabbitCoverage(CodeRabbitCoverageStatus.REVIEWED),
        changed_files=changed_files
        if changed_files is not None
        else (
            ChangedFile(
                path=TARGET_PATH,
                status="modified",
                sha="d" * 40,
                patch="@@ -1 +1 @@\n-old\n+new",
            ),
        ),
    )


class FakeSnapshotProvider:
    def __init__(self, snapshots: tuple[PullRequestFeedbackSnapshot, ...]) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, tuple[FeedbackRevision, ...]]] = []

    def __call__(
        self,
        policy: RepositoryPolicy,
        previous_feedback: tuple[FeedbackRevision, ...],
    ) -> tuple[PullRequestFeedbackSnapshot, ...]:
        self.calls.append((policy.base_repo, previous_feedback))
        return tuple(
            snapshot
            for snapshot in self.snapshots
            if snapshot.pull_request.repository == policy.base_repo
        )


class LeaseProbeSnapshotProvider(FakeSnapshotProvider):
    def __init__(
        self,
        snapshots: tuple[PullRequestFeedbackSnapshot, ...],
        *,
        state_path: Path,
        probe_at: datetime,
    ) -> None:
        super().__init__(snapshots)
        self.state_path = state_path
        self.probe_at = probe_at
        self.rival_acquired: bool | None = None

    def __call__(
        self,
        policy: RepositoryPolicy,
        previous_feedback: tuple[FeedbackRevision, ...],
    ) -> tuple[PullRequestFeedbackSnapshot, ...]:
        with GuardianState(self.state_path) as rival:
            self.rival_acquired = rival.acquire_lease(
                name="guardian:poll",
                owner="rival",
                ttl_seconds=30,
                now=self.probe_at,
            )
        return super().__call__(policy, previous_feedback)


class FakeCodexDriver:
    model = "test-model"

    def __init__(
        self, *, confidence: float = 0.99, error: Exception | None = None
    ) -> None:
        self.confidence = confidence
        self.error = error
        self.calls = []
        self.api_keys: list[str | None] = []

    def run(
        self,
        task,
        *,
        api_key: str | None = None,
        attempt_observer=None,
        success_observer=None,
    ) -> CodexResult:
        if attempt_observer is not None:
            attempt_observer(1, "started", None)
        self.calls.append(task)
        self.api_keys.append(api_key)
        if self.error is not None:
            if attempt_observer is not None:
                attempt_observer(1, "failed", None)
            raise self.error
        manifest = json.loads((task.evidence_dir / "manifest.json").read_text())
        feedback_id = manifest["feedback_ids"][0]
        result = CodexResult(
            schema_version=1,
            summary="One safe value correction.",
            feedback=(
                GuardianFeedbackDecision(
                    feedback_id=feedback_id,
                    verdict="apply",
                    confidence=self.confidence,
                    rationale="The proposed wording is more idiomatic.",
                    replacements=(
                        GuardianReplacement(
                            path=TARGET_PATH,
                            key="greeting",
                            expected_value="Старый %0 был отклонён (%1). %2 %3",
                            proposed_value="Отправка в %0 отклонена (%1). %2 %3",
                        ),
                    ),
                ),
            ),
            recurrence_candidates=(),
            attempts=1,
            usage=CodexUsage(input_tokens=100, output_tokens=20, cost_usd=0.25),
        )
        if success_observer is not None:
            success_observer(1, result.usage, result)
        if attempt_observer is not None:
            attempt_observer(1, "succeeded", result.usage)
        return result


class RecurrenceCodexDriver(FakeCodexDriver):
    def run(
        self,
        task,
        *,
        api_key: str | None = None,
        attempt_observer=None,
        success_observer=None,
    ) -> CodexResult:
        transformed: CodexResult | None = None

        def persist_recurrence(attempt, usage, result):
            nonlocal transformed
            feedback_id = result.feedback[0].feedback_id
            transformed = replace(
                result,
                recurrence_candidates=(
                    GuardianRecurrenceCandidate(
                        scope="pipeline_code",
                        summary="Reject this review defect before publication.",
                        evidence_feedback_ids=(feedback_id,),
                    ),
                ),
            )
            if success_observer is not None:
                success_observer(attempt, usage, transformed)

        result = super().run(
            task,
            api_key=api_key,
            attempt_observer=attempt_observer,
            success_observer=persist_recurrence,
        )
        feedback_id = result.feedback[0].feedback_id
        return transformed or replace(
            result,
            recurrence_candidates=(
                GuardianRecurrenceCandidate(
                    scope="pipeline_code",
                    summary="Reject this review defect before publication.",
                    evidence_feedback_ids=(feedback_id,),
                ),
            ),
        )


class RetryingCodexDriver(FakeCodexDriver):
    def run(
        self,
        task,
        *,
        api_key: str | None = None,
        attempt_observer=None,
        success_observer=None,
    ) -> CodexResult:
        assert attempt_observer is not None
        attempt_observer(1, "started", None)
        attempt_observer(
            1,
            "failed",
            CodexUsage(input_tokens=40, output_tokens=5, cost_usd=0.10),
        )
        result = super().run(
            task,
            api_key=api_key,
            attempt_observer=lambda _attempt, phase, usage: attempt_observer(
                2, phase, usage
            ),
            success_observer=(
                None
                if success_observer is None
                else lambda _attempt, usage, successful: success_observer(
                    2, usage, successful
                )
            ),
        )
        return replace(result, attempts=2)


class FakePreventionRunner:
    def __init__(
        self,
        *,
        result: PreventionBatchOutcome | None = None,
        error: Exception | None = None,
        sequence: list[str] | None = None,
    ) -> None:
        self.result = result or PreventionBatchOutcome()
        self.error = error
        self.sequence = sequence
        self.begin_calls = 0
        self.recover_calls: list[dict[str, object]] = []
        self.propose_calls: list[dict[str, object]] = []

    def begin_poll(self) -> None:
        self.begin_calls += 1

    def recover(self, **kwargs: object) -> PreventionBatchOutcome:
        self.recover_calls.append(dict(kwargs))
        return PreventionBatchOutcome()

    def propose(self, **kwargs: object) -> PreventionBatchOutcome:
        self.propose_calls.append(dict(kwargs))
        if self.sequence is not None:
            self.sequence.append("prevention")
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeWorkspace:
    path: Path
    original_sha: str
    sequence: list[str]
    commits: int = 0
    publications: int = 0

    def commit_validated_changes(self, **kwargs) -> CommitResult:
        self.sequence.append("commit")
        self.commits += 1
        assert kwargs["sign"] is True
        assert kwargs["expected_paths"] == (TARGET_PATH,)
        return CommitResult(
            commit_sha=COMMIT_SHA,
            parent_sha=self.original_sha,
            changed_paths=(TARGET_PATH,),
            signature_verified=True,
        )

    def publish_commit(self, commit: CommitResult, **kwargs) -> PublicationResult:
        assert commit.commit_sha == COMMIT_SHA
        assert kwargs["require_signature"] is True
        kwargs["before_push"]()
        self.sequence.append("publish")
        self.publications += 1
        return PublicationResult(
            ref="refs/heads/localize/russian",
            previous_sha=self.original_sha,
            commit_sha=commit.commit_sha,
        )


class FakeCheckoutFactory:
    def __init__(
        self, base_tree: Path, head_tree: Path, tmp_path: Path, sequence: list[str]
    ) -> None:
        self.base_tree = base_tree
        self.head_tree = head_tree
        self.tmp_path = tmp_path
        self.sequence = sequence
        self.workspaces: list[FakeWorkspace] = []
        self.counter = 0

    @contextmanager
    def __call__(self, revision) -> Iterator[FakeWorkspace]:
        self.counter += 1
        source = self.base_tree if revision.owner == "acme" else self.head_tree
        destination = self.tmp_path / f"checkout-{self.counter}"
        shutil.copytree(source, destination)
        workspace = FakeWorkspace(destination, revision.sha, self.sequence)
        self.workspaces.append(workspace)
        try:
            yield workspace
        finally:
            shutil.rmtree(destination)


class FakeBroker:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.verify_calls = []
        self.verify_error_on_call: int | None = None
        self.reply_calls = []
        self.reply_error: Exception | None = None

    def verify_pull(self, **kwargs):
        self.sequence.append("verify")
        self.verify_calls.append(kwargs)
        if self.verify_error_on_call == len(self.verify_calls):
            raise RuntimeError("fresh pull authority changed")
        return _pull()

    def post_commit_reply(self, **kwargs):
        self.sequence.append("reply")
        self.reply_calls.append(kwargs)
        if self.reply_error is not None:
            raise self.reply_error
        kwargs["before_create"]()
        assert kwargs["expected_head_sha"] == COMMIT_SHA
        assert kwargs["commit_sha"] == COMMIT_SHA
        return object()


@pytest.fixture
def runtime(tmp_path: Path):
    base = tmp_path / "base-template"
    head = tmp_path / "head-template"
    base.mkdir()
    head.mkdir()
    _write_tree(base)
    # The untrusted head config is deliberately incompatible. The controller
    # and its source string has hostile placeholder drift. The controller must
    # use the exact base config and source while validating head target files.
    _write_tree(head, head_config_source_locale="xx")
    (head / "l10n/messages_en.properties").write_text(
        "greeting=Untrusted source rewrite %9\n",
        encoding="utf-8",
    )
    sequence: list[str] = []
    checkout = FakeCheckoutFactory(base, head, tmp_path, sequence)
    provider = FakeSnapshotProvider((_snapshot(),))
    broker = FakeBroker(sequence)
    return base, head, checkout, provider, broker, sequence


def _controller(
    *,
    tmp_path: Path,
    state: GuardianState,
    config: GuardianConfig,
    checkout: FakeCheckoutFactory,
    provider: FakeSnapshotProvider,
    driver: FakeCodexDriver,
    broker: FakeBroker,
    prevention_runner: FakePreventionRunner | None = None,
) -> GuardianController:
    return GuardianController(
        config=config,
        state=state,
        snapshot_provider=provider,
        checkout_factory=checkout,
        codex_driver=driver,
        model_credential_provider=lambda: "scoped-model-key",
        write_broker_factory=lambda _policy: broker,
        prevention_runner=prevention_runner,
        publish_credential_environment=lambda: {"GIT_ASKPASS": "/safe/helper"},
        evidence_root=tmp_path / "evidence",
        now=lambda: NOW,
    )


def test_propose_prevention_runs_before_translation_publish_and_records_draft(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    prevention = FakePreventionRunner(
        result=PreventionBatchOutcome(
            drafts=(
                PreventionDraftResult(
                    number=91,
                    html_url="https://github.com/guardian/pipeline/pull/91",
                    candidate_sha="e" * 40,
                    created=True,
                ),
            ),
        ),
        sequence=sequence,
    )
    policy = replace(_policy(), prevention=_prevention_policy())
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            driver=RecurrenceCodexDriver(),
            broker=broker,
            prevention_runner=prevention,
        ).poll_once()

        assert outcome.runs_completed == 1
        assert outcome.prevention_drafts_created == 1
        assert outcome.prevention_failures == ()
        assert prevention.begin_calls == 1
        assert len(prevention.recover_calls) == 1
        assert len(prevention.propose_calls) == 1
        propose = prevention.propose_calls[0]
        assert propose["policy"] == policy
        assert propose["evidence_revision_ids"] == {"review_comment:44": 1}
        assert sequence == [
            "prevention",
            "commit",
            "verify",
            "verify",
            "publish",
            "reply",
        ]


def test_propose_prevention_authentication_failure_opens_poll_circuit(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    prevention = FakePreventionRunner(
        error=CodexAuthenticationError("secret model detail"),
        sequence=sequence,
    )
    policy = replace(_policy(), prevention=_prevention_policy())
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            driver=RecurrenceCodexDriver(),
            broker=broker,
            prevention_runner=prevention,
        ).poll_once()

        assert outcome.authentication_circuit_open is True
        assert outcome.runs_failed == 1
        assert outcome.applied_commits == ()
        assert sequence == ["prevention"]
        assert state.pending_event_revisions(
            mode=GuardianMode.PROPOSE_PREVENTION
        )


def test_apply_then_propose_assesses_recurrence_without_reapplying_translation(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    policy = replace(_policy(), prevention=_prevention_policy())
    prevention = FakePreventionRunner(sequence=sequence)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        applied = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            driver=FakeCodexDriver(),
            broker=broker,
        ).poll_once()
        assert applied.applied_commits == (COMMIT_SHA,)

        sequence.clear()
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)
        recurrence_driver = RecurrenceCodexDriver()
        proposed = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            driver=recurrence_driver,
            broker=broker,
            prevention_runner=prevention,
        ).poll_once()

        assert proposed.runs_completed == 1
        assert proposed.applied_commits == ()
        assert len(recurrence_driver.calls) == 1
        assert len(prevention.propose_calls) == 1
        assert sequence == ["prevention"]
        assert state.pending_event_revisions(
            mode=GuardianMode.PROPOSE_PREVENTION
        ) == ()


def test_github_authentication_failure_stops_later_repositories(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, _provider, broker, _sequence = runtime
    calls: list[str] = []

    def failing_provider(policy, _previous):
        calls.append(policy.base_repo)
        raise GitHubAuthenticationError("redacted auth failure")

    policies = (_policy(repository="acme/first"), _policy(repository="acme/second"))
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE, policies=policies),
            checkout=checkout,
            provider=failing_provider,  # type: ignore[arg-type]
            driver=FakeCodexDriver(),
            broker=broker,
        ).poll_once()

    assert outcome.authentication_circuit_open is True
    assert calls == ["acme/first"]
    assert outcome.repositories_polled == 0


def test_incomplete_prevention_candidate_leaves_feedback_retryable(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    prevention = FakePreventionRunner(
        result=PreventionBatchOutcome(
            deferred=1,
            failures=("PreventionPolicyError",),
        ),
        sequence=sequence,
    )
    policy = replace(_policy(), prevention=_prevention_policy())
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            driver=RecurrenceCodexDriver(),
            broker=broker,
            prevention_runner=prevention,
        ).poll_once()

        assert outcome.runs_failed == 1
        assert outcome.prevention_items_deferred == 1
        assert outcome.prevention_failures == ("PreventionPolicyError",)
        assert outcome.failures == ("PreventionRuntimeError",)
        assert sequence == ["prevention"]
        assert state.pending_event_revisions(
            mode=GuardianMode.PROPOSE_PREVENTION
        )


def test_propose_prevention_recovers_without_new_feedback(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, _provider, broker, _sequence = runtime
    provider = FakeSnapshotProvider(())
    recovered = PreventionDraftResult(
        number=92,
        html_url="https://github.com/guardian/pipeline/pull/92",
        candidate_sha="f" * 40,
        created=True,
    )
    prevention = FakePreventionRunner()
    prevention.recover = lambda **_kwargs: PreventionBatchOutcome(  # type: ignore[method-assign]
        drafts=(recovered,)
    )
    policy = replace(_policy(), prevention=_prevention_policy())
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PROPOSE_PREVENTION, policies=(policy,)),
            checkout=checkout,
            provider=provider,
            driver=RecurrenceCodexDriver(),
            broker=broker,
            prevention_runner=prevention,
        ).poll_once()

    assert outcome.prevention_drafts_created == 1
    assert outcome.runs_started == 0
    assert prevention.begin_calls == 1


def test_observe_uses_exact_base_config_and_is_revision_idempotent(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )

        first = controller.poll_once()
        second = controller.poll_once()

        assert first.runs_completed == 1
        assert second.runs_started == 0
        assert len(driver.calls) == 1
        assert driver.api_keys == ["scoped-model-key"]
        assert broker.verify_calls == []
        assert sequence == []
        assert state.budget_committed_for_day(NOW.date()) == pytest.approx(0.25)
        assert provider.calls[1][1][0].body == "Use the idiomatic wording."


def test_chatgpt_subscription_ignores_api_helper_and_records_call_not_cost(
    tmp_path: Path,
    runtime,
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    config = GuardianConfig(
        repositories=(_policy(),),
        mode=GuardianMode.OBSERVE,
        limits=GuardianLimits(max_model_calls_per_day=2),
        runtime=GuardianRuntime(codex_auth_mode=CodexAuthMode.CHATGPT),
    )
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=config,
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )

        def forbidden_helper() -> str:
            raise AssertionError("subscription mode must not read an API key")

        controller.model_credential_provider = forbidden_helper
        outcome = controller.poll_once()

        assert outcome.runs_completed == 1
        assert driver.api_keys == [None]
        assert state.model_calls_committed_for_day(NOW.date()) == 1
        assert state.cost_for_day(NOW.date()) == 0


def test_subscription_daily_call_limit_stops_before_model_invocation(
    tmp_path: Path,
    runtime,
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    config = GuardianConfig(
        repositories=(_policy(),),
        mode=GuardianMode.OBSERVE,
        limits=GuardianLimits(max_model_calls_per_day=1),
        runtime=GuardianRuntime(codex_auth_mode=CodexAuthMode.CHATGPT),
    )
    with GuardianState(tmp_path / "state.sqlite3") as state:
        prior_run = state.start_run(
            repository="acme/widgets",
            locale="ru",
            mode=GuardianMode.OBSERVE,
            started_at=NOW,
        )
        assert state.try_reserve_model_call(
            run_id=prior_run,
            daily_limit=1,
            model="test-model",
            purpose="assessment",
            reserved_at=NOW,
        ) is not None

        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=config,
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_failed == 1
        assert driver.calls == []
        assert state.pending_event_revisions()


def test_each_codex_retry_has_an_independent_budget_reservation_and_cost(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=RetryingCodexDriver(),
            broker=broker,
        ).poll_once()

        assert outcome.runs_completed == 1
        assert outcome.failures == ()
        assert state.budget_committed_for_day(NOW.date()) == Decimal("0.35")
        with state._connection:  # Exact audit contract, not a public query surface.
            reservations = state._connection.execute(
                "SELECT status FROM budget_reservations ORDER BY reservation_id"
            ).fetchall()
        assert [row["status"] for row in reservations] == ["settled", "settled"]


def test_poll_lease_outlives_one_configured_operation_timeout(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, _provider, broker, _sequence = runtime
    state_path = tmp_path / "state.sqlite3"
    timeout_seconds = 10
    provider = LeaseProbeSnapshotProvider(
        (_snapshot(),),
        state_path=state_path,
        probe_at=NOW + timedelta(seconds=timeout_seconds + 1),
    )
    limits = GuardianLimits(
        run_timeout_seconds=timeout_seconds,
        daily_cost_limit_usd=2,
        model_call_reservation_usd=1,
    )
    with GuardianState(state_path) as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE, limits=limits),
            checkout=checkout,
            provider=provider,
            driver=FakeCodexDriver(),
            broker=broker,
        ).poll_once()

        assert provider.rival_acquired is False
        assert outcome.runs_completed == 1
        assert outcome.failures == ()


def test_edited_feedback_is_a_new_revision_and_reassessed(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        controller.poll_once()
        provider.snapshots = (
            _snapshot(
                feedback=(
                    _feedback(
                        body="Use the revised idiomatic wording.",
                        updated_at="2026-08-30T11:00:00Z",
                    ),
                ),
            ),
        )

        outcome = controller.poll_once()

        assert outcome.runs_completed == 1
        assert len(driver.calls) == 2
        assert len(state.latest_event_revisions(repository="acme/widgets")) == 1


def test_mode_escalation_reuses_the_exact_cached_assessment(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PREPARE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.prepared_value_edits == 1
        assert len(driver.calls) == 1
        assert state.pending_event_revisions(mode=GuardianMode.PREPARE) == ()


def test_crash_after_model_success_reuses_durable_result_without_rebilling(
    tmp_path: Path,
    runtime,
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )

        def crash_after_persistence(*_args, **_kwargs):
            raise RuntimeError("simulated post-model crash")

        first.assessment_converter = crash_after_persistence
        failed = first.poll_once()

        assert failed.runs_failed == 1
        assert len(driver.calls) == 1
        assert state.cost_for_day(NOW.date()) == Decimal("0.25")
        assert state.pending_event_revisions(mode=GuardianMode.OBSERVE)

        recovered = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert recovered.runs_completed == 1
        assert len(driver.calls) == 1
        assert driver.api_keys == ["scoped-model-key"]
        assert state.cost_for_day(NOW.date()) == Decimal("0.25")
        assert state.pending_event_revisions(mode=GuardianMode.OBSERVE) == ()


def test_new_edit_supersedes_retryable_old_revision_before_assessment(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    no_budget = GuardianLimits(
        daily_cost_limit_usd=0,
        model_call_reservation_usd=1,
    )
    with GuardianState(tmp_path / "state.sqlite3") as state:
        _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE, limits=no_budget),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()
        provider.snapshots = (
            _snapshot(
                feedback=(
                    _feedback(
                        body="Use the newly edited wording.",
                        updated_at="2026-08-30T11:30:00Z",
                    ),
                )
            ),
        )

        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_completed == 1
        assert len(driver.calls) == 1
        assert state.pending_event_revisions(mode=GuardianMode.OBSERVE) == ()


def test_trusted_deletion_tombstone_resolves_retryable_old_revision_without_model(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    no_budget = GuardianLimits(
        daily_cost_limit_usd=0,
        model_call_reservation_usd=1,
    )
    with GuardianState(tmp_path / "state.sqlite3") as state:
        _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE, limits=no_budget),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()
        provider.snapshots = (
            _snapshot(
                feedback=(
                    replace(
                        _feedback(),
                        body="",
                        deleted=True,
                        updated_at="2026-08-30T11:45:00Z",
                    ),
                )
            ),
        )

        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_completed == 1
        assert driver.calls == []
        latest = state.latest_event_revisions(repository="acme/widgets")
        assert latest[0].deleted is True
        assert state.pending_event_revisions(mode=GuardianMode.OBSERVE) == ()


def test_prepare_validates_in_ephemeral_checkout_without_remote_writes(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.PREPARE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_completed == 1
        assert outcome.prepared_value_edits == 1
        assert outcome.applied_commits == ()
        assert sequence == []
        assert broker.verify_calls == []
        assert all(workspace.commits == 0 for workspace in checkout.workspaces)


def test_apply_signs_then_reverifies_immediately_before_normal_publish_and_reply(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.applied_commits == (COMMIT_SHA,)
        assert sequence == ["commit", "verify", "verify", "publish", "reply"]
        assert broker.verify_calls == [
            {
                "pull_number": 12,
                "expected_head_sha": HEAD_SHA,
                "expected_base_sha": BASE_SHA,
            },
            {
                "pull_number": 12,
                "expected_head_sha": HEAD_SHA,
                "expected_base_sha": BASE_SHA,
            },
        ]
        assert broker.reply_calls[0]["event_revision_id"].isdigit()
        assert state.pending_publications() == ()
        publication = state.replied_publication_for_head(
            repository="acme/widgets",
            pr_number=12,
            head_sha=COMMIT_SHA,
        )
        assert publication is not None
        assert broker.reply_calls[0]["action_id"] == publication.publication_key


def test_lost_lease_immediately_before_push_prevents_all_remote_mutations(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        original_refresh = state.refresh_lease

        def lose_after_remote_verification(**kwargs):
            if sequence and sequence[-1] == "verify":
                return False
            return original_refresh(**kwargs)

        state.refresh_lease = lose_after_remote_verification  # type: ignore[method-assign]
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_failed == 1
        assert outcome.applied_commits == ()
        assert sequence == ["commit", "verify"]
        assert broker.reply_calls == []
        assert state.pending_publications()[0].phase == "prepared"


def test_base_movement_after_initial_verification_prevents_push_and_reply(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    broker.verify_error_on_call = 2
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=FakeCodexDriver(),
            broker=broker,
        ).poll_once()

        assert outcome.runs_failed == 1
        assert outcome.applied_commits == ()
        assert sequence == ["commit", "verify", "verify"]
        assert broker.reply_calls == []
        assert state.pending_publications()[0].phase == "prepared"


def test_published_commit_reply_recovers_idempotently_without_a_second_model_call(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    broker.reply_error = RuntimeError("connection dropped after publication")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert first.runs_failed == 1
        assert state.pending_publications()[0].phase == "published"
        broker.reply_error = None
        provider.snapshots = (
            _snapshot(pull=_pull(head_sha=COMMIT_SHA)),
        )

        second = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert second.failures == ()
        assert len(driver.calls) == 1
        assert state.pending_publications() == ()
        assert state.pending_event_revisions(
            mode=GuardianMode.APPLY_OWNED_TRANSLATIONS
        ) == ()


def test_recovery_abandons_publication_when_the_base_revision_moved(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    broker.reply_error = RuntimeError("connection dropped after publication")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert first.runs_failed == 1
        assert state.pending_publications()[0].phase == "published"
        broker.reply_error = None
        replies_before_recovery = len(broker.reply_calls)
        provider.snapshots = (
            _snapshot(
                pull=_pull(
                    head_sha=COMMIT_SHA,
                    base_sha="e" * 40,
                )
            ),
        )

        second = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert second.failures == ()
        assert len(broker.reply_calls) == replies_before_recovery + 1
        assert len(driver.calls) == 2
        stale_reply, fresh_reply = broker.reply_calls
        assert stale_reply["expected_base_sha"] == BASE_SHA
        assert fresh_reply["expected_base_sha"] == "e" * 40
        assert fresh_reply["action_id"] != stale_reply["action_id"]
        assert state.pending_publications() == ()
        assert state.pending_event_revisions(
            mode=GuardianMode.APPLY_OWNED_TRANSLATIONS
        ) == ()


def test_recovery_abandons_publication_when_pull_is_no_longer_open_or_authorized(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    broker.reply_error = RuntimeError("connection dropped after publication")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()
        assert first.runs_failed == 1
        assert state.pending_publications()[0].phase == "published"

        broker.reply_error = None
        provider.snapshots = ()
        second = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert second.failures == ()
        assert len(driver.calls) == 1
        assert state.pending_publications() == ()


def test_lost_lease_prevents_a_recovered_status_reply(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    broker.reply_error = RuntimeError("connection dropped after publication")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()
        assert first.runs_failed == 1
        assert state.pending_publications()[0].phase == "published"

        broker.reply_error = None
        replies_before_recovery = len(broker.reply_calls)
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)
        original_refresh = state.refresh_lease
        refresh_calls = 0

        def lose_during_recovery(**kwargs):
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 2:
                return False
            return original_refresh(**kwargs)

        state.refresh_lease = lose_during_recovery  # type: ignore[method-assign]
        second = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert second.failures == ("RuntimeError",)
        assert len(broker.reply_calls) == replies_before_recovery
        assert state.pending_publications()[0].phase == "published"


@pytest.mark.parametrize("non_write_mode", [GuardianMode.OBSERVE, GuardianMode.PREPARE])
def test_non_write_modes_never_recover_a_pending_write(
    tmp_path: Path, runtime, non_write_mode: GuardianMode
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    broker.reply_error = RuntimeError("connection dropped after publication")
    with GuardianState(tmp_path / "state.sqlite3") as state:
        first = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()
        assert first.runs_failed == 1
        assert state.pending_publications()[0].phase == "published"

        broker.reply_error = None
        replies_before_observe = len(broker.reply_calls)
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)
        _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(non_write_mode),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert len(broker.reply_calls) == replies_before_observe
        assert state.pending_publications()[0].phase == "published"


def test_guardian_owned_head_does_not_reassess_unchanged_feedback(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )
        controller.poll_once()
        provider.snapshots = (_snapshot(pull=_pull(head_sha=COMMIT_SHA)),)

        outcome = controller.poll_once()

        assert outcome.runs_completed == 1
        assert len(driver.calls) == 1
        assert state.pending_event_revisions(
            mode=GuardianMode.APPLY_OWNED_TRANSLATIONS
        ) == ()


def test_apply_below_confidence_threshold_never_mutates_or_contacts_write_broker(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime
    driver = FakeCodexDriver(confidence=0.89)
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.APPLY_OWNED_TRANSLATIONS),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.prepared_value_edits == 0
        assert outcome.applied_commits == ()
        assert sequence == []
        assert broker.verify_calls == []


def test_daily_budget_is_reserved_before_model_and_denial_remains_retryable(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    limits = GuardianLimits(
        daily_cost_limit_usd=0,
        model_call_reservation_usd=1,
    )
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE, limits=limits),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_failed == 1
        assert driver.calls == []
        assert len(state.pending_event_revisions(mode=GuardianMode.OBSERVE)) == 1
        assert state.latest_health("guardian").status == "failed"


def test_unknown_model_cost_keeps_conservative_reservation(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    original_run = driver.run

    def no_cost(
        task,
        *,
        api_key=None,
        attempt_observer=None,
        success_observer=None,
    ):
        def without_reported_cost(attempt, phase, usage):
            if phase == "succeeded":
                usage = CodexUsage(input_tokens=100, output_tokens=20)
            if attempt_observer is not None:
                attempt_observer(attempt, phase, usage)

        result = original_run(
            task,
            api_key=api_key,
            attempt_observer=without_reported_cost,
            success_observer=(
                None
                if success_observer is None
                else lambda attempt, _usage, successful: success_observer(
                    attempt,
                    CodexUsage(input_tokens=100, output_tokens=20),
                    replace(
                        successful,
                        usage=CodexUsage(input_tokens=100, output_tokens=20),
                    ),
                )
            ),
        )
        return replace(result, usage=CodexUsage(input_tokens=100, output_tokens=20))

    driver.run = no_cost  # type: ignore[method-assign]
    with GuardianState(tmp_path / "state.sqlite3") as state:
        _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert state.cost_for_day(NOW.date()) == 0
        assert state.budget_committed_for_day(NOW.date()) == 1


def test_codex_authentication_failure_opens_circuit_and_stops_later_repositories(
    tmp_path: Path, runtime
) -> None:
    base, head, checkout, _provider, broker, _sequence = runtime
    second_policy = _policy(repository="other/widgets")
    second_snapshot = replace(
        _snapshot(),
        repository_identity=GitHubRepositoryIdentity("other/widgets", 42, False),
        pull_request=replace(_pull(), repository="other/widgets"),
        feedback=(replace(_feedback(), repository="other/widgets"),),
    )
    provider = FakeSnapshotProvider((_snapshot(), second_snapshot))
    driver = FakeCodexDriver(error=CodexAuthenticationError("credential rejected"))
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(
                GuardianMode.OBSERVE,
                policies=(_policy(), second_policy),
            ),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.authentication_circuit_open is True
        assert [repository for repository, _previous in provider.calls] == [
            "acme/widgets"
        ]
        assert state.latest_health("codex").status == "failed"
        assert state.budget_committed_for_day(NOW.date()) == 1


def test_codex_capacity_failure_opens_model_circuit_and_stops_later_repositories(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, _provider, broker, _sequence = runtime
    second_policy = _policy(repository="other/widgets")
    second_snapshot = replace(
        _snapshot(),
        repository_identity=GitHubRepositoryIdentity("other/widgets", 42, False),
        pull_request=replace(_pull(), repository="other/widgets"),
        feedback=(replace(_feedback(), repository="other/widgets"),),
    )
    provider = FakeSnapshotProvider((_snapshot(), second_snapshot))
    driver = FakeCodexDriver(error=CodexCapacityError("allowance exhausted"))
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(
                GuardianMode.OBSERVE,
                policies=(_policy(), second_policy),
            ),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.model_circuit_open is True
        assert outcome.authentication_circuit_open is False
        assert [repository for repository, _previous in provider.calls] == [
            "acme/widgets"
        ]
        assert state.latest_health("codex").status == "failed"


def test_model_credential_helper_failure_opens_circuit_without_reserving_cost(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, _provider, broker, _sequence = runtime
    second_policy = _policy(repository="other/widgets")
    second_snapshot = replace(
        _snapshot(),
        repository_identity=GitHubRepositoryIdentity("other/widgets", 42, False),
        pull_request=replace(_pull(), repository="other/widgets"),
        feedback=(replace(_feedback(), repository="other/widgets"),),
    )
    provider = FakeSnapshotProvider((_snapshot(), second_snapshot))
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(
                GuardianMode.OBSERVE,
                policies=(_policy(), second_policy),
            ),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        )

        def unavailable_credential() -> str:
            raise RuntimeError("keychain locked")

        controller.model_credential_provider = unavailable_credential
        outcome = controller.poll_once()

        assert outcome.authentication_circuit_open is True
        assert [repository for repository, _previous in provider.calls] == [
            "acme/widgets"
        ]
        assert driver.calls == []
        assert state.budget_committed_for_day(NOW.date()) == 0


def test_raw_feedback_body_is_purged_after_retention_window(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        old_event = state.record_feedback_event(
            # Build through the same authorization path once, then make its
            # raw observation old while retaining the immutable revision.
            FeedbackEvent(
                repository="acme/widgets",
                pr_number=12,
                kind="review_comment",
                event_id="44",
                author="native-reviewer",
                author_id=101,
                author_type="User",
                body="Use the idiomatic wording.",
                head_sha=HEAD_SHA,
                base_sha=BASE_SHA,
                locale="ru",
                updated_at="2026-08-30T10:00:00Z",
                path=TARGET_PATH,
                line=1,
                html_url="https://github.com/acme/widgets/pull/12#discussion_r44",
            ),
            observed_at=NOW - timedelta(days=91),
        )
        _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert state.get_event_revision(old_event.revision_id).body is None


def test_head_checkout_failure_finishes_run_and_keeps_feedback_retryable(
    tmp_path: Path, runtime
) -> None:
    _base, _head, checkout, provider, broker, sequence = runtime

    class FailingHeadCheckout(FakeCheckoutFactory):
        @contextmanager
        def __call__(self, revision):
            if revision.sha == HEAD_SHA:
                raise RuntimeError("head fetch failed")
            with super().__call__(revision) as workspace:
                yield workspace

    failing = FailingHeadCheckout(
        checkout.base_tree,
        checkout.head_tree,
        tmp_path,
        sequence,
    )
    driver = FakeCodexDriver()
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=failing,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_failed == 1
        assert outcome.failures == ("RuntimeError",)
        assert driver.calls == []
        assert state.pending_event_revisions(mode=GuardianMode.OBSERVE)
        assert state.reconcile_incomplete_runs(before=NOW + timedelta(days=1)) == ()


def test_non_target_or_wrong_source_locale_pr_is_rejected_before_model(
    tmp_path: Path, runtime
) -> None:
    base, _head, checkout, provider, broker, _sequence = runtime
    driver = FakeCodexDriver()
    base_config = yaml.safe_load((base / ".localize/config.yaml").read_text())
    base_config["source_locale"] = "de"
    base_config["localization_layout"]["source_locale"] = "de"
    (base / ".localize/config.yaml").write_text(yaml.safe_dump(base_config))
    provider.snapshots = (
        _snapshot(
            changed_files=(
                ChangedFile(path="src/App.java", status="modified", sha="d" * 40),
            )
        ),
    )
    with GuardianState(tmp_path / "state.sqlite3") as state:
        outcome = _controller(
            tmp_path=tmp_path,
            state=state,
            config=_config(GuardianMode.OBSERVE),
            checkout=checkout,
            provider=provider,
            driver=driver,
            broker=broker,
        ).poll_once()

        assert outcome.runs_started == 0
        assert outcome.failures
        assert driver.calls == []
