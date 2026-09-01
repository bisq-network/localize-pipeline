"""One bounded, revision-aware orchestration pass for Localize Guardian.

The controller is intentionally dependency-injected at every credential or
network boundary.  It coordinates trusted primitives, but does not itself know
how an operator retrieves a GitHub token or a Codex API key.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
from typing import ContextManager, Protocol
from uuid import uuid4

import yaml

from localize.guardian.authorization import authorize_feedback
from localize.guardian.codex import (
    CodexAuthenticationError,
    CodexCapacityError,
    CodexResult,
    CodexTask,
    CodexUsage,
    RESULT_SCHEMA_PATH,
    parse_cached_codex_result,
    serialize_codex_result,
    to_guardian_assessments,
)
from localize.guardian.evidence import EvidenceBundle, build_evidence_bundle
from localize.guardian.github import (
    ChangedFile,
    FeedbackKind,
    FeedbackRevision,
    GitHubAuthenticationError,
    GitHubWriteBroker,
    PullRequestFeedbackSnapshot,
)
from localize.guardian.models import (
    CodexAuthMode,
    FeedbackEvent,
    GuardianAssessment,
    GuardianConfig,
    GuardianMode,
    ProposedReplacement,
    RecurrenceCandidate,
    RepositoryPolicy,
)
from localize.guardian.path_globs import matches_any_path_glob
from localize.guardian.policy import PatchPolicyError, PatchResult, apply_replacements
from localize.guardian.prevention_runtime import (
    PreventionBatchOutcome,
    PreventionRuntimeError,
)
from localize.guardian.state import EventRevision, GuardianState
from localize.guardian.workspace import (
    ExactRevision,
    GuardianWorkspace,
)
from localize.localization_profiles import (
    LocalizationProfile,
    load_localization_profiles,
)


_UTC = timezone.utc
_SUPPORTED_CHANGED_FILE_STATUSES = frozenset({"added", "modified"})
_ASSESSMENT_PROMPT = (
    "Read INSTRUCTIONS.md and the complete sanitized evidence bundle. "
    "Assess every manifest feedback ID exactly once and write only the "
    "schema-conforming result."
)
_ASSESSMENT_CACHE_VERSION = 1


def _assessment_cache_key(
    bundle: EvidenceBundle,
    *,
    model: str,
    reasoning_effort: str,
) -> str:
    schema_hash = hashlib.sha256(RESULT_SCHEMA_PATH.read_bytes()).hexdigest()
    identity = json.dumps(
        {
            "cache_version": _ASSESSMENT_CACHE_VERSION,
            "evidence_hash": bundle.evidence_hash,
            "model": model,
            "prompt": _ASSESSMENT_PROMPT,
            "reasoning_effort": reasoning_effort,
            "schema_hash": schema_hash,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class SnapshotProvider(Protocol):
    """Read-only GitHub intake supplied by the runtime credential boundary."""

    def __call__(
        self,
        policy: RepositoryPolicy,
        previous_feedback: tuple[FeedbackRevision, ...],
    ) -> Sequence[PullRequestFeedbackSnapshot]: ...


class CheckoutFactory(Protocol):
    """Materialize one exact base or head revision."""

    def __call__(
        self, revision: ExactRevision
    ) -> ContextManager[GuardianWorkspace]: ...


class CodexRunner(Protocol):
    """Minimum driver surface used by the controller."""

    model: str

    def run(
        self,
        task: CodexTask,
        *,
        api_key: str | None = None,
        attempt_observer: Callable[[int, str, CodexUsage | None], None] | None = None,
        success_observer: Callable[[int, CodexUsage | None, CodexResult], None]
        | None = None,
    ) -> CodexResult: ...


class WriteBrokerFactory(Protocol):
    """Create the narrow GitHub broker for one already-authorized policy."""

    def __call__(self, policy: RepositoryPolicy) -> GitHubWriteBroker: ...


class PreventionRunner(Protocol):
    """Narrow draft-only prevention surface supplied by production wiring."""

    def begin_poll(self) -> None: ...

    def recover(
        self,
        *,
        policy: RepositoryPolicy,
        observed_at: datetime,
        require_live_lease: Callable[[], None],
    ) -> PreventionBatchOutcome: ...

    def propose(
        self,
        *,
        policy: RepositoryPolicy,
        recurrence_candidates: Sequence[RecurrenceCandidate],
        evidence_revision_ids: Mapping[str, int],
        run_id: str,
        observed_at: datetime,
        require_live_lease: Callable[[], None],
    ) -> PreventionBatchOutcome: ...


@dataclass(frozen=True)
class PollOutcome:
    """Secret-free summary of one bounded poll."""

    lease_acquired: bool
    repositories_polled: int = 0
    pull_requests_seen: int = 0
    feedback_revisions_recorded: int = 0
    runs_started: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    prepared_value_edits: int = 0
    applied_commits: tuple[str, ...] = ()
    prevention_drafts_created: int = 0
    prevention_items_skipped: int = 0
    prevention_items_deferred: int = 0
    prevention_failures: tuple[str, ...] = ()
    authentication_circuit_open: bool = False
    model_circuit_open: bool = False
    raw_bodies_purged: int = 0
    failures: tuple[str, ...] = ()


@dataclass
class _PollAccumulator:
    lease_acquired: bool = True
    repositories_polled: int = 0
    pull_requests_seen: int = 0
    feedback_revisions_recorded: int = 0
    runs_started: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    prepared_value_edits: int = 0
    applied_commits: list[str] = field(default_factory=list)
    prevention_drafts_created: int = 0
    prevention_items_skipped: int = 0
    prevention_items_deferred: int = 0
    prevention_failures: list[str] = field(default_factory=list)
    authentication_circuit_open: bool = False
    model_circuit_open: bool = False
    raw_bodies_purged: int = 0
    failures: list[str] = field(default_factory=list)

    def freeze(self) -> PollOutcome:
        return PollOutcome(
            lease_acquired=self.lease_acquired,
            repositories_polled=self.repositories_polled,
            pull_requests_seen=self.pull_requests_seen,
            feedback_revisions_recorded=self.feedback_revisions_recorded,
            runs_started=self.runs_started,
            runs_completed=self.runs_completed,
            runs_failed=self.runs_failed,
            prepared_value_edits=self.prepared_value_edits,
            applied_commits=tuple(self.applied_commits),
            prevention_drafts_created=self.prevention_drafts_created,
            prevention_items_skipped=self.prevention_items_skipped,
            prevention_items_deferred=self.prevention_items_deferred,
            prevention_failures=tuple(self.prevention_failures),
            authentication_circuit_open=self.authentication_circuit_open,
            model_circuit_open=self.model_circuit_open,
            raw_bodies_purged=self.raw_bodies_purged,
            failures=tuple(self.failures),
        )


@dataclass(frozen=True)
class _TargetScope:
    config_path: Path
    path_locales: Mapping[str, str]
    changed_files: Mapping[str, ChangedFile]


class _AuthenticationCircuit(RuntimeError):
    """Internal signal that no more model calls may run in this poll."""


class _ModelCircuit(RuntimeError):
    """Internal signal that provider capacity cannot recover during this poll."""


class _BudgetUnavailable(RuntimeError):
    """Internal signal raised before an unbudgeted model attempt can start."""


class _ModelCallLimitUnavailable(RuntimeError):
    """Internal signal raised before exceeding the daily provider-call cap."""


class _ModelCredentialUnavailable(RuntimeError):
    """Internal signal that the model credential helper failed closed."""


def _safe_failure_name(error: BaseException) -> str:
    """Return an audit-safe failure identifier without untrusted text."""

    return type(error).__name__


def _utc_now() -> datetime:
    return datetime.now(_UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Guardian controller clock must be timezone-aware.")
    return value.astimezone(_UTC)


def _split_repository(full_name: str) -> tuple[str, str]:
    parts = full_name.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("Repository identity must use owner/name form.")
    return parts[0], parts[1]


def _exact_revisions(
    policy: RepositoryPolicy,
    snapshot: PullRequestFeedbackSnapshot,
    *,
    github_host: str,
) -> tuple[ExactRevision, ExactRevision]:
    pull = snapshot.pull_request
    base_owner, base_name = _split_repository(policy.base_repo)
    head_owner, head_name = _split_repository(pull.head_repository)
    base = ExactRevision(
        host=github_host,
        owner=base_owner,
        repository=base_name,
        ref=f"refs/heads/{pull.base_ref}",
        sha=pull.base_sha,
    )
    head = ExactRevision(
        host=github_host,
        owner=head_owner,
        repository=head_name,
        ref=f"refs/heads/{pull.head_ref}",
        sha=pull.head_sha,
    )
    return base, head


def _trusted_file(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("Trusted pipeline config path is unsafe.")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Trusted pipeline config path contains a symbolic link.")
    resolved_root = root.resolve()
    resolved = current.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            "Trusted pipeline config escapes the exact base checkout."
        ) from exc
    if not resolved.is_file():
        raise ValueError("Trusted pipeline config is not a regular file.")
    return resolved


def _load_base_profiles(
    config_path: Path,
    *,
    expected_source_locale: str,
) -> tuple[tuple[LocalizationProfile, ...], tuple[str, ...]]:
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Trusted pipeline config could not be loaded.") from exc
    if not isinstance(config, Mapping):
        raise ValueError("Trusted pipeline config must contain a mapping.")
    raw_locales = config.get("supported_locales")
    if not isinstance(raw_locales, list):
        raise ValueError("Trusted pipeline config supported_locales must be a list.")
    locale_codes: list[str] = []
    for item in raw_locales:
        if isinstance(item, str) and item:
            locale_codes.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("code"), str):
            code = str(item["code"])
            if code:
                locale_codes.append(code)
    if not locale_codes:
        raise ValueError("Trusted pipeline config has no target locales.")
    try:
        profiles = load_localization_profiles(config)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Trusted pipeline config has invalid localization profiles."
        ) from exc
    if str(config.get("source_locale") or "en") != expected_source_locale or any(
        profile.localization_layout.source_locale != expected_source_locale
        for profile in profiles
    ):
        raise ValueError(
            "Trusted pipeline config source locale does not match Guardian policy."
        )
    return profiles, tuple(dict.fromkeys(locale_codes))


def _canonical_changed_path(path: str) -> str:
    if (
        not path
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("Pull request contains an unsafe changed path.")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or pure.as_posix() != path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("Pull request contains an unsafe changed path.")
    return path


def _target_scope(
    *,
    base_root: Path,
    policy: RepositoryPolicy,
    changed_files: Sequence[ChangedFile],
) -> _TargetScope:
    config_path = _trusted_file(base_root, policy.pipeline_config_path)
    profiles, locale_codes = _load_base_profiles(
        config_path,
        expected_source_locale=policy.source_locale,
    )
    if not changed_files:
        raise ValueError("Pull request has no changed files.")

    path_locales: dict[str, str] = {}
    files_by_path: dict[str, ChangedFile] = {}
    for changed_file in changed_files:
        path = _canonical_changed_path(changed_file.path)
        if path in files_by_path:
            raise ValueError("Pull request repeats a changed path.")
        if (
            changed_file.status not in _SUPPORTED_CHANGED_FILE_STATUSES
            or changed_file.previous_path is not None
        ):
            raise ValueError("Pull request uses an unsupported changed-file operation.")
        if not matches_any_path_glob(path, policy.allowed_path_globs):
            raise ValueError("Pull request changes a path outside Guardian policy.")

        matches: list[str] = []
        for profile in profiles:
            layout = profile.localization_layout
            file_format = profile.localization_format
            if not layout.is_target_file(path, locale_codes, file_format):
                continue
            locale = layout.extract_locale(path, locale_codes, file_format)
            if locale and locale != policy.source_locale:
                matches.append(locale)
        if len(matches) != 1:
            raise ValueError(
                "Every pull-request path must identify exactly one target locale."
            )
        path_locales[path] = matches[0]
        files_by_path[path] = changed_file
    return _TargetScope(
        config_path=config_path,
        path_locales=path_locales,
        changed_files=files_by_path,
    )


def _stored_feedback(
    revisions: Sequence[EventRevision],
) -> tuple[FeedbackRevision, ...]:
    previous: list[FeedbackRevision] = []
    for revision in revisions:
        try:
            kind = FeedbackKind(revision.kind)
        except ValueError:
            continue
        timestamp = revision.updated_at or revision.observed_at.isoformat()
        previous.append(
            FeedbackRevision(
                repository=revision.repository,
                pull_number=revision.pr_number,
                kind=kind,
                source_id=revision.event_id,
                node_id=None,
                author_login=revision.author,
                author_id=revision.author_id,
                author_type=revision.author_type,
                body=revision.body or "",
                created_at=timestamp,
                updated_at=timestamp,
                html_url=revision.html_url or "",
                path=revision.path,
                line=revision.line,
                commit_id=revision.head_sha,
                deleted=revision.deleted,
            )
        )
    return tuple(previous)


def _trusted_tombstones(
    *,
    policy: RepositoryPolicy,
    snapshot: PullRequestFeedbackSnapshot,
    previous: Sequence[EventRevision],
) -> tuple[FeedbackEvent, ...]:
    previous_by_object = {
        (revision.kind, revision.event_id): revision for revision in previous
    }
    pull = snapshot.pull_request
    events: list[FeedbackEvent] = []
    for revision in snapshot.feedback:
        if not revision.deleted:
            continue
        prior = previous_by_object.get((revision.kind.value, revision.source_id))
        if prior is None or prior.deleted or revision.author_id is None:
            continue
        trusted_actor = policy.trusted_reviewer_by_id(prior.locale, revision.author_id)
        if trusted_actor is None:
            trusted_actor = policy.trusted_bot_by_id(prior.locale, revision.author_id)
        if trusted_actor is None or trusted_actor.type != revision.author_type:
            continue
        events.append(
            FeedbackEvent(
                repository=policy.base_repo,
                pr_number=pull.number,
                kind=revision.kind.value,
                event_id=revision.source_id,
                author=revision.author_login,
                author_id=revision.author_id,
                author_type=revision.author_type,
                body="",
                head_sha=pull.head_sha,
                base_sha=pull.base_sha,
                locale=prior.locale,
                updated_at=revision.updated_at,
                path=revision.path if revision.path is not None else prior.path,
                line=revision.line if revision.line is not None else prior.line,
                html_url=revision.html_url or prior.html_url,
                deleted=True,
            )
        )
    return tuple(events)


def _diff_text(
    paths: Sequence[str],
    changed_files: Mapping[str, ChangedFile],
) -> str:
    sections: list[str] = []
    for path in sorted(paths):
        patch = changed_files[path].patch
        sections.append(
            f"diff --git a/{path} b/{path}\n"
            + (patch.rstrip("\n") + "\n" if patch else "[patch unavailable]\n")
        )
    return "".join(sections)


def _source_values(bundle: EvidenceBundle) -> dict[tuple[str, str], str]:
    try:
        payload = json.loads(
            (bundle.root / "localization.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Controller-generated localization evidence is unreadable."
        ) from exc
    result: dict[tuple[str, str], str] = {}
    if not isinstance(payload, list):
        raise ValueError("Controller-generated localization evidence is malformed.")
    for file_data in payload:
        if not isinstance(file_data, Mapping):
            raise ValueError("Controller-generated localization evidence is malformed.")
        path = file_data.get("path")
        entries = file_data.get("entries")
        if not isinstance(path, str) or not isinstance(entries, Mapping):
            raise ValueError("Controller-generated localization evidence is malformed.")
        for key, values in entries.items():
            if (
                not isinstance(key, str)
                or not isinstance(values, Mapping)
                or not isinstance(values.get("source"), str)
            ):
                raise ValueError(
                    "Controller-generated localization evidence is malformed."
                )
            result[(path, key)] = str(values["source"])
    return result


def _eligible_replacements(
    assessments: Sequence[GuardianAssessment],
    *,
    minimum_confidence: float,
    excluded_feedback_ids: frozenset[str] = frozenset(),
) -> tuple[ProposedReplacement, ...]:
    return tuple(
        replacement
        for assessment in assessments
        if assessment.feedback_id not in excluded_feedback_ids
        and assessment.verdict == "apply"
        and assessment.confidence >= minimum_confidence
        for replacement in assessment.replacements
        if replacement.confidence >= minimum_confidence
    )


def _lease_ttl_seconds(config: GuardianConfig) -> int:
    """Outlive any one bounded operation plus scheduling and cleanup slack."""

    timeout = config.limits.run_timeout_seconds
    return max(timeout * 2, timeout + 300)


class GuardianController:
    """Coordinate one safe polling pass across the configured repositories."""

    def __init__(
        self,
        *,
        config: GuardianConfig,
        state: GuardianState,
        snapshot_provider: SnapshotProvider,
        checkout_factory: CheckoutFactory,
        codex_driver: CodexRunner,
        model_credential_provider: Callable[[], str | None] | None = None,
        write_broker_factory: WriteBrokerFactory | None = None,
        prevention_runner: PreventionRunner | None = None,
        publish_credential_environment: Callable[[], Mapping[str, str]] | None = None,
        evidence_root: Path | None = None,
        now: Callable[[], datetime] = _utc_now,
        evidence_builder: Callable[..., EvidenceBundle] = build_evidence_bundle,
        replacement_applier: Callable[..., PatchResult] = apply_replacements,
        assessment_converter: Callable[
            ..., tuple[GuardianAssessment, ...]
        ] = to_guardian_assessments,
        github_host: str = "github.com",
        signing_key: str | None = None,
        signing_environment: Mapping[str, str] | None = None,
    ) -> None:
        if (
            config.mode
            in {
                GuardianMode.APPLY_OWNED_TRANSLATIONS,
                GuardianMode.PROPOSE_PREVENTION,
            }
            and write_broker_factory is None
        ):
            raise ValueError(
                "Apply-capable Guardian modes require a write broker factory."
            )
        if (
            config.mode is GuardianMode.PROPOSE_PREVENTION
            and prevention_runner is None
        ):
            raise ValueError(
                "Propose-prevention mode requires a prevention runner."
            )
        self.config = config
        self.state = state
        self.snapshot_provider = snapshot_provider
        self.checkout_factory = checkout_factory
        self.codex_driver = codex_driver
        self.model_credential_provider = model_credential_provider
        self.write_broker_factory = write_broker_factory
        self.prevention_runner = prevention_runner
        self.publish_credential_environment = publish_credential_environment
        self.evidence_root = evidence_root
        self.now = now
        self.evidence_builder = evidence_builder
        self.replacement_applier = replacement_applier
        self.assessment_converter = assessment_converter
        self.github_host = github_host
        self.signing_key = (
            signing_key if signing_key is not None else config.runtime.signing_key
        )
        self.signing_environment = signing_environment

    def poll_once(self) -> PollOutcome:
        """Run one finite poll, never retrying at the orchestration layer."""

        observed_at = _as_utc(self.now())
        owner = str(uuid4())
        lease_name = "guardian:poll"
        acquired = self.state.acquire_lease(
            name=lease_name,
            owner=owner,
            ttl_seconds=_lease_ttl_seconds(self.config),
            now=observed_at,
        )
        if not acquired:
            return PollOutcome(lease_acquired=False)

        outcome = _PollAccumulator()
        try:
            self.state.reconcile_incomplete_runs(
                before=observed_at
                - timedelta(seconds=self.config.limits.run_timeout_seconds),
                reconciled_at=observed_at,
            )
            if self.config.mode is GuardianMode.PROPOSE_PREVENTION:
                # Constructor enforces this dependency for the only mode that
                # may use it.
                assert self.prevention_runner is not None
                self.prevention_runner.begin_poll()
            for policy in self.config.repositories:
                if outcome.authentication_circuit_open or outcome.model_circuit_open:
                    break
                try:
                    self._require_live_lease(owner)
                    if self.config.mode is GuardianMode.PROPOSE_PREVENTION:
                        assert self.prevention_runner is not None
                        self._record_prevention_outcome(
                            self.prevention_runner.recover(
                                policy=policy,
                                observed_at=observed_at,
                                require_live_lease=lambda: self._require_live_lease(
                                    owner
                                ),
                            ),
                            outcome=outcome,
                        )
                    previous_revisions = self.state.latest_event_revisions(
                        repository=policy.base_repo
                    )
                    snapshots = tuple(
                        self.snapshot_provider(
                            policy,
                            _stored_feedback(previous_revisions),
                        )
                    )
                    outcome.repositories_polled += 1
                    self._recover_publications(
                        policy=policy,
                        snapshots=snapshots,
                        observed_at=observed_at,
                        lease_owner=owner,
                    )
                    for snapshot in snapshots:
                        outcome.pull_requests_seen += 1
                        self._process_snapshot(
                            policy=policy,
                            snapshot=snapshot,
                            observed_at=observed_at,
                            lease_owner=owner,
                            outcome=outcome,
                        )
                except _AuthenticationCircuit:
                    outcome.authentication_circuit_open = True
                except _ModelCircuit:
                    outcome.model_circuit_open = True
                except GitHubAuthenticationError:
                    outcome.authentication_circuit_open = True
                    self.state.record_health(
                        component="github",
                        status="failed",
                        message=(
                            "GitHub authentication failed; further GitHub calls "
                            "stopped."
                        ),
                        details={"circuit_open": True},
                        checked_at=observed_at,
                    )
                except Exception as exc:
                    outcome.failures.append(_safe_failure_name(exc))
                    self.state.record_health(
                        component="controller",
                        status="failed",
                        message="Guardian repository poll failed closed.",
                        details={"failure_type": _safe_failure_name(exc)},
                        checked_at=observed_at,
                    )
        finally:
            try:
                retention_cutoff = observed_at - timedelta(
                    days=self.config.limits.raw_retention_days
                )
                outcome.raw_bodies_purged = self.state.purge_raw_event_bodies(
                    before=retention_cutoff
                )
                self.state.purge_assessment_results(before=retention_cutoff)
            finally:
                self.state.release_lease(name=lease_name, owner=owner)
        poll_ok = (
            not outcome.failures
            and not outcome.authentication_circuit_open
            and not outcome.model_circuit_open
            and outcome.runs_failed == 0
        )
        self.state.record_health(
            component="guardian",
            status="ok" if poll_ok else "failed",
            message=(
                "Guardian bounded poll completed."
                if poll_ok
                else "Guardian bounded poll completed with failed work."
            ),
            details={
                "repositories_polled": outcome.repositories_polled,
                "pull_requests_seen": outcome.pull_requests_seen,
                "runs_failed": outcome.runs_failed,
                "prevention_drafts_created": outcome.prevention_drafts_created,
                "prevention_items_deferred": outcome.prevention_items_deferred,
                "prevention_failures": tuple(outcome.prevention_failures),
                "authentication_circuit_open": outcome.authentication_circuit_open,
                "model_circuit_open": outcome.model_circuit_open,
                "failure_types": tuple(outcome.failures),
            },
            checked_at=observed_at,
        )
        return outcome.freeze()

    def _recover_publications(
        self,
        *,
        policy: RepositoryPolicy,
        snapshots: Sequence[PullRequestFeedbackSnapshot],
        observed_at: datetime,
        lease_owner: str,
    ) -> None:
        """Finish an idempotent reply after a confirmed push or process crash."""

        if (
            self.config.mode
            not in {
                GuardianMode.APPLY_OWNED_TRANSLATIONS,
                GuardianMode.PROPOSE_PREVENTION,
            }
            or self.write_broker_factory is None
        ):
            return
        snapshots_by_pr = {
            snapshot.pull_request.number: snapshot for snapshot in snapshots
        }
        for publication in self.state.pending_publications(
            repository=policy.base_repo
        ):
            metadata = {
                "run_id": publication.run_id,
                "repository": publication.repository,
                "pr_number": publication.pr_number,
                "original_head_sha": publication.original_head_sha,
                "base_sha": publication.base_sha,
                "commit_sha": publication.commit_sha,
                "event_revision_ids": publication.event_revision_ids,
                "occurred_at": observed_at,
            }

            def abandon(reason: str) -> None:
                self.state.record_publication_event(
                    **metadata,
                    phase="abandoned",
                )
                run = self.state.get_run(publication.run_id)
                if run.status == "running":
                    for revision_id in publication.event_revision_ids:
                        self.state.record_action(
                            run_id=publication.run_id,
                            event_revision_id=revision_id,
                            action=run.mode.value,
                            status="failed",
                            details={
                                "outcome": "publication_abandoned",
                                "reason": reason,
                            },
                            occurred_at=observed_at,
                        )
                    self.state.finish_run(
                        publication.run_id,
                        status="failed",
                        summary="Abandoned a publication outside current PR authority.",
                        finished_at=observed_at,
                    )

            snapshot = snapshots_by_pr.get(publication.pr_number)
            if snapshot is None:
                abandon("pull_request_not_open_or_authorized")
                continue
            pull = snapshot.pull_request
            if pull.head_sha == publication.original_head_sha:
                abandon("guardian_commit_not_present")
                continue
            if pull.head_sha != publication.commit_sha:
                abandon("pull_request_head_moved")
                continue
            if pull.base_sha != publication.base_sha:
                abandon("pull_request_base_moved")
                continue

            broker = self.write_broker_factory(policy)
            self._require_live_lease(lease_owner)
            broker.post_commit_reply(
                pull_number=publication.pr_number,
                expected_head_sha=publication.commit_sha,
                expected_base_sha=publication.base_sha,
                commit_sha=publication.commit_sha,
                action_id=publication.publication_key,
                event_revision_id=str(min(publication.event_revision_ids)),
                before_create=lambda: self._require_live_lease(lease_owner),
            )
            self.state.record_publication_event(
                **metadata,
                phase="replied",
            )
            run = self.state.get_run(publication.run_id)
            for revision_id in publication.event_revision_ids:
                self.state.record_action(
                    run_id=publication.run_id,
                    event_revision_id=revision_id,
                    action=run.mode.value,
                    status="completed",
                    details={
                        "outcome": "applied",
                        "commit_sha": publication.commit_sha,
                        "recovered_reply": True,
                    },
                    occurred_at=observed_at,
                )
            if run.status == "running":
                self.state.finish_run(
                    publication.run_id,
                    status="completed",
                    summary="Recovered a confirmed Guardian publication.",
                    finished_at=observed_at,
                )

    def _process_snapshot(
        self,
        *,
        policy: RepositoryPolicy,
        snapshot: PullRequestFeedbackSnapshot,
        observed_at: datetime,
        lease_owner: str,
        outcome: _PollAccumulator,
    ) -> None:
        self._require_live_lease(lease_owner)
        base_revision, head_revision = _exact_revisions(
            policy,
            snapshot,
            github_host=self.github_host,
        )
        previous = self.state.latest_event_revisions(
            repository=policy.base_repo,
            pr_number=snapshot.pull_request.number,
        )
        with self.checkout_factory(base_revision) as base_workspace:
            scope = _target_scope(
                base_root=base_workspace.path,
                policy=policy,
                changed_files=snapshot.changed_files,
            )
            authorized = authorize_feedback(
                policy=policy,
                snapshot=snapshot,
                path_locales=scope.path_locales,
                changed_locales=tuple(sorted(set(scope.path_locales.values()))),
            )
            current_events = (
                *authorized.events,
                *_trusted_tombstones(
                    policy=policy,
                    snapshot=snapshot,
                    previous=previous,
                ),
            )
            replied_publication = self.state.replied_publication_for_head(
                repository=policy.base_repo,
                pr_number=snapshot.pull_request.number,
                head_sha=snapshot.pull_request.head_sha,
            )
            addressed_signatures: set[tuple[str, str, str]] = set()
            translation_applied_signatures: set[tuple[str, str, str]] = set()
            if replied_publication is not None:
                publication_mode = self.state.get_run(
                    replied_publication.run_id
                ).mode
                signature_target = (
                    translation_applied_signatures
                    if self.config.mode is GuardianMode.PROPOSE_PREVENTION
                    and publication_mode is GuardianMode.APPLY_OWNED_TRANSLATIONS
                    else addressed_signatures
                )
                for revision_id in replied_publication.event_revision_ids:
                    prior_revision = self.state.get_event_revision(revision_id)
                    if prior_revision is not None:
                        signature_target.add(
                            (
                                prior_revision.kind,
                                prior_revision.event_id,
                                prior_revision.revision_hash,
                            )
                        )
            current: dict[tuple[str, str], tuple[FeedbackEvent, EventRevision]] = {}
            for event in current_events:
                revision = self.state.record_feedback_event(
                    event,
                    observed_at=observed_at,
                )
                if revision.is_new:
                    outcome.feedback_revisions_recorded += 1
                current[(event.kind, event.event_id)] = (event, revision)

            pending = tuple(
                revision
                for revision in self.state.pending_event_revisions(
                    repository=policy.base_repo,
                    mode=self.config.mode,
                )
                if revision.pr_number == snapshot.pull_request.number
            )
            current_revision_ids = {
                revision.revision_id for _event, revision in current.values()
            }
            superseded = tuple(
                revision
                for revision in pending
                if (revision.kind, revision.event_id) in current
                and revision.revision_id not in current_revision_ids
            )
            current_pending = tuple(
                (event, revision)
                for event, revision in current.values()
                if revision.revision_id
                in {pending_revision.revision_id for pending_revision in pending}
            )
            if not superseded and not current_pending:
                return

            locale_label = ",".join(
                sorted(
                    {
                        revision.locale
                        for revision in (
                            *superseded,
                            *(item[1] for item in current_pending),
                        )
                    }
                )
            )
            run_id = self.state.start_run(
                repository=policy.base_repo,
                locale=locale_label,
                mode=self.config.mode,
                started_at=observed_at,
            )
            outcome.runs_started += 1
            for revision in superseded:
                self.state.record_action(
                    run_id=run_id,
                    event_revision_id=revision.revision_id,
                    action=self.config.mode.value,
                    status="skipped",
                    details={"outcome": "superseded_revision"},
                    occurred_at=observed_at,
                )

            deleted = tuple(
                (event, revision)
                for event, revision in current_pending
                if event.deleted
            )
            already_addressed = tuple(
                (event, revision)
                for event, revision in current_pending
                if (
                    event.kind,
                    event.event_id,
                    revision.revision_hash,
                )
                in addressed_signatures
            )
            actionable = tuple(
                (event, revision)
                for event, revision in current_pending
                if not event.deleted
                and revision.revision_id
                not in {item[1].revision_id for item in already_addressed}
            )
            translation_suppressed_feedback_ids = frozenset(
                event.feedback_id
                for event, revision in actionable
                if (
                    event.kind,
                    event.event_id,
                    revision.revision_hash,
                )
                in translation_applied_signatures
            )
            for _event, revision in deleted:
                self.state.record_action(
                    run_id=run_id,
                    event_revision_id=revision.revision_id,
                    action=self.config.mode.value,
                    status="skipped",
                    details={"outcome": "deleted"},
                    occurred_at=observed_at,
                )
            for _event, revision in already_addressed:
                self.state.record_action(
                    run_id=run_id,
                    event_revision_id=revision.revision_id,
                    action=self.config.mode.value,
                    status="skipped",
                    details={"outcome": "already_applied_by_guardian"},
                    occurred_at=observed_at,
                )
            if not actionable:
                self.state.finish_run(
                    run_id,
                    status="completed",
                    summary=(
                        "Resolved only superseded, deleted, or already-applied "
                        "feedback revisions."
                    ),
                    finished_at=observed_at,
                )
                outcome.runs_completed += 1
                return

            try:
                with self.checkout_factory(head_revision) as head_workspace:
                    self._assess_and_act(
                        policy=policy,
                        snapshot=snapshot,
                        scope=scope,
                        base_workspace=base_workspace,
                        head_workspace=head_workspace,
                        actionable=actionable,
                        translation_suppressed_feedback_ids=(
                            translation_suppressed_feedback_ids
                        ),
                        run_id=run_id,
                        observed_at=observed_at,
                        require_live_lease=lambda: self._require_live_lease(
                            lease_owner
                        ),
                        lease_owner=lease_owner,
                        outcome=outcome,
                    )
            except Exception:
                # Checkout/materialization can fail before _assess_and_act owns
                # the run. Never strand it as running; that would suppress a
                # truthful immediate status until the next stale-run sweep.
                if self.state.get_run(run_id).status == "running":
                    self._fail_actions(
                        run_id=run_id,
                        revisions=tuple(revision for _event, revision in actionable),
                        outcome_name="head_checkout_failure",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary="Exact head checkout failed closed.",
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                raise

    def _assessment_result(
        self,
        *,
        bundle: EvidenceBundle,
        policy: RepositoryPolicy,
        snapshot: PullRequestFeedbackSnapshot,
        run_id: str,
    ) -> CodexResult:
        model = self.codex_driver.model
        reasoning_effort = self.config.runtime.codex_reasoning_effort
        cache_key = _assessment_cache_key(
            bundle,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        cached = self.state.cached_assessment_result(
            cache_key=cache_key,
            repository=policy.base_repo,
            pr_number=snapshot.pull_request.number,
            head_sha=snapshot.pull_request.head_sha,
            base_sha=snapshot.pull_request.base_sha,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if cached is not None:
            return parse_cached_codex_result(cached)

        api_key = None
        if self.config.runtime.codex_auth_mode is CodexAuthMode.API_KEY:
            if self.model_credential_provider is None:
                raise _ModelCredentialUnavailable
            try:
                api_key = self.model_credential_provider()
            except Exception:
                raise _ModelCredentialUnavailable from None

        attempt_reservations: dict[int, int] = {}
        attempt_calls: dict[int, int] = {}
        api_billed = self.config.runtime.codex_auth_mode is CodexAuthMode.API_KEY

        def account_attempt(
            attempt: int,
            phase: str,
            usage: CodexUsage | None,
        ) -> None:
            accounted_at = _as_utc(self.now())
            if phase == "started":
                call_id = self.state.try_reserve_model_call(
                    run_id=run_id,
                    daily_limit=self.config.limits.max_model_calls_per_day,
                    model=model,
                    reserved_at=accounted_at,
                    purpose="assessment",
                )
                if call_id is None:
                    raise _ModelCallLimitUnavailable(
                        "Daily model call limit was unavailable."
                    )
                attempt_calls[attempt] = call_id
                if not api_billed:
                    return
                reservation_usd = self.config.limits.model_call_reservation_usd
                daily_limit_usd = self.config.limits.daily_cost_limit_usd
                if reservation_usd is None or daily_limit_usd is None:
                    raise RuntimeError("API billing limits are not configured.")
                reservation_id = self.state.try_reserve_budget(
                    run_id=run_id,
                    amount_usd=reservation_usd,
                    daily_limit_usd=daily_limit_usd,
                    model=model,
                    reserved_at=accounted_at,
                )
                if reservation_id is None:
                    self.state.finalize_model_call(
                        call_id,
                        status="cancelled",
                        finalized_at=accounted_at,
                    )
                    attempt_calls.pop(attempt, None)
                    raise _BudgetUnavailable(
                        "Daily model budget was unavailable."
                    )
                attempt_reservations[attempt] = reservation_id
                return
            if phase == "succeeded":
                if attempt in attempt_calls or attempt in attempt_reservations:
                    raise RuntimeError(
                        "Codex success was not durably cached before completion."
                    )
                return

            call_id = attempt_calls.pop(attempt, None)
            if call_id is None:
                raise RuntimeError(
                    "Codex attempt finished without a model call reservation."
                )
            self.state.finalize_model_call(
                call_id,
                status="cancelled" if phase == "not_started" else "unknown",
                finalized_at=accounted_at,
            )
            if not api_billed:
                return
            reservation_id = attempt_reservations.pop(attempt, None)
            if reservation_id is None:
                raise RuntimeError(
                    "Codex attempt finished without a budget reservation."
                )
            if phase == "not_started":
                self.state.settle_budget_reservation(
                    reservation_id,
                    actual_cost_usd=0,
                    settled_at=accounted_at,
                )
            elif usage is not None and usage.cost_usd is not None:
                self.state.settle_budget_reservation(
                    reservation_id,
                    actual_cost_usd=usage.cost_usd,
                    input_tokens=usage.input_tokens or 0,
                    output_tokens=usage.output_tokens or 0,
                    settled_at=accounted_at,
                )
            else:
                self.state.mark_budget_reservation_unknown(
                    reservation_id,
                    marked_at=accounted_at,
                )

        def persist_success(
            attempt: int,
            usage: CodexUsage | None,
            result: CodexResult,
        ) -> None:
            call_id = attempt_calls.pop(attempt, None)
            if call_id is None:
                raise RuntimeError("Codex success has no matching model call reservation.")
            serialized_result = serialize_codex_result(result)
            created_at = _as_utc(self.now())
            if not api_billed:
                self.state.cache_assessment_result(
                    cache_key=cache_key,
                    repository=policy.base_repo,
                    pr_number=snapshot.pull_request.number,
                    head_sha=snapshot.pull_request.head_sha,
                    base_sha=snapshot.pull_request.base_sha,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    result_json=serialized_result,
                    created_at=created_at,
                )
                self.state.finalize_model_call(
                    call_id,
                    status="completed",
                    finalized_at=created_at,
                )
                return
            reservation_id = attempt_reservations.pop(attempt, None)
            if reservation_id is None:
                raise RuntimeError(
                    "Codex success has no matching budget reservation."
                )
            self.state.cache_assessment_and_settle_budget(
                cache_key=cache_key,
                repository=policy.base_repo,
                pr_number=snapshot.pull_request.number,
                head_sha=snapshot.pull_request.head_sha,
                base_sha=snapshot.pull_request.base_sha,
                model=model,
                reasoning_effort=reasoning_effort,
                result_json=serialized_result,
                reservation_id=reservation_id,
                actual_cost_usd=(usage.cost_usd if usage is not None else None),
                input_tokens=(usage.input_tokens or 0 if usage is not None else 0),
                output_tokens=(usage.output_tokens or 0 if usage is not None else 0),
                created_at=created_at,
            )
            self.state.finalize_model_call(
                call_id,
                status="completed",
                finalized_at=created_at,
            )

        return self.codex_driver.run(
            CodexTask(
                prompt=_ASSESSMENT_PROMPT,
                evidence_dir=bundle.root,
            ),
            api_key=api_key,
            attempt_observer=account_attempt,
            success_observer=persist_success,
        )

    def _assess_and_act(
        self,
        *,
        policy: RepositoryPolicy,
        snapshot: PullRequestFeedbackSnapshot,
        scope: _TargetScope,
        base_workspace: GuardianWorkspace,
        head_workspace: GuardianWorkspace,
        actionable: Sequence[tuple[FeedbackEvent, EventRevision]],
        translation_suppressed_feedback_ids: frozenset[str],
        run_id: str,
        observed_at: datetime,
        require_live_lease: Callable[[], None],
        lease_owner: str,
        outcome: _PollAccumulator,
    ) -> None:
        events = tuple(event for event, _revision in actionable)
        revisions = tuple(revision for _event, revision in actionable)
        event_locales = {event.locale for event in events}
        changed_paths = tuple(
            sorted(
                path
                for path, locale in scope.path_locales.items()
                if locale in event_locales
            )
        )
        try:
            evidence_parent = self.evidence_root
            if evidence_parent is not None:
                evidence_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(
                prefix="localize-guardian-evidence-",
                dir=evidence_parent,
            ) as temporary_directory:
                bundle = self.evidence_builder(
                    destination=Path(temporary_directory) / "bundle",
                    repo_root=head_workspace.path,
                    trusted_pipeline_config_path=scope.config_path,
                    repository=policy.base_repo,
                    pr_number=snapshot.pull_request.number,
                    head_sha=snapshot.pull_request.head_sha,
                    base_sha=snapshot.pull_request.base_sha,
                    feedback=events,
                    changed_paths=changed_paths,
                    allowed_path_globs=policy.allowed_path_globs,
                    diff_text=_diff_text(changed_paths, scope.changed_files),
                    trusted_config_root=base_workspace.path,
                    trusted_source_root=base_workspace.path,
                    expected_source_locale=policy.source_locale,
                )
                try:
                    result = self._assessment_result(
                        bundle=bundle,
                        policy=policy,
                        snapshot=snapshot,
                        run_id=run_id,
                    )
                except _ModelCredentialUnavailable:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="model_credential_unavailable",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary="Model credential helper failed; circuit opened.",
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    self.state.record_health(
                        component="codex",
                        status="failed",
                        message=(
                            "Model credential helper failed; further model calls stopped."
                        ),
                        details={"circuit_open": True},
                        checked_at=observed_at,
                    )
                    raise _AuthenticationCircuit from None
                except _BudgetUnavailable:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="daily_budget_unavailable",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary="Daily model budget was unavailable.",
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    return
                except _ModelCallLimitUnavailable:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="daily_model_call_limit_unavailable",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary="Daily model call limit was unavailable.",
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    return
                except CodexAuthenticationError:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="codex_authentication_failed",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary="Codex authentication failed; circuit opened.",
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    self.state.record_health(
                        component="codex",
                        status="failed",
                        message="Codex authentication failed; further model calls stopped.",
                        details={"circuit_open": True},
                        checked_at=observed_at,
                    )
                    raise _AuthenticationCircuit from None
                except CodexCapacityError:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="codex_capacity_unavailable",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary="Codex capacity was unavailable; circuit opened.",
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    self.state.record_health(
                        component="codex",
                        status="failed",
                        message="Codex capacity unavailable; further model calls stopped.",
                        details={"circuit_open": True, "reason": "capacity"},
                        checked_at=observed_at,
                    )
                    raise _ModelCircuit from None

                self._require_live_lease(lease_owner)
                assessments = self.assessment_converter(
                    result,
                    feedback_events=events,
                    source_values=_source_values(bundle),
                )

            recurrence_candidates = tuple(
                candidate
                for assessment in assessments
                for candidate in assessment.recurrence_candidates
            )
            if (
                self.config.mode is GuardianMode.PROPOSE_PREVENTION
                and recurrence_candidates
            ):
                assert self.prevention_runner is not None
                try:
                    prevention_outcome = self.prevention_runner.propose(
                        policy=policy,
                        recurrence_candidates=recurrence_candidates,
                        evidence_revision_ids={
                            event.feedback_id: revision.revision_id
                            for event, revision in actionable
                        },
                        run_id=run_id,
                        observed_at=observed_at,
                        require_live_lease=require_live_lease,
                    )
                except CodexAuthenticationError:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="prevention_codex_authentication_failed",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary=(
                            "Prevention Codex authentication failed; circuit opened."
                        ),
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    self.state.record_health(
                        component="codex",
                        status="failed",
                        message=(
                            "Prevention Codex authentication failed; further model "
                            "calls stopped."
                        ),
                        details={"circuit_open": True},
                        checked_at=observed_at,
                    )
                    raise _AuthenticationCircuit from None
                except CodexCapacityError:
                    self._fail_actions(
                        run_id=run_id,
                        revisions=revisions,
                        outcome_name="prevention_codex_capacity_unavailable",
                        observed_at=observed_at,
                    )
                    self.state.finish_run(
                        run_id,
                        status="failed",
                        summary=(
                            "Prevention Codex capacity was unavailable; circuit opened."
                        ),
                        finished_at=observed_at,
                    )
                    outcome.runs_failed += 1
                    self.state.record_health(
                        component="codex",
                        status="failed",
                        message=(
                            "Prevention Codex capacity unavailable; further model "
                            "calls stopped."
                        ),
                        details={"circuit_open": True, "reason": "capacity"},
                        checked_at=observed_at,
                    )
                    raise _ModelCircuit from None
                self._record_prevention_outcome(
                    prevention_outcome,
                    outcome=outcome,
                )
                if prevention_outcome.failures or prevention_outcome.deferred:
                    # Leave the feedback revisions retryable. Successful drafts
                    # are durably deduplicated, so a later run can continue the
                    # remaining bounded candidates without duplicating them.
                    raise PreventionRuntimeError(
                        "Prevention candidates remain incomplete."
                    )

            replacements = _eligible_replacements(
                assessments,
                minimum_confidence=self.config.limits.min_apply_confidence,
                excluded_feedback_ids=translation_suppressed_feedback_ids,
            )
            patch_result = PatchResult(changed_files=(), changed_keys=())
            if self.config.mode is not GuardianMode.OBSERVE and replacements:
                patch_result = self.replacement_applier(
                    repo_root=head_workspace.path,
                    pipeline_config_path=scope.config_path,
                    allowed_paths=policy.allowed_path_globs,
                    replacements=replacements,
                    max_changes=self.config.limits.max_value_edits_per_run,
                    trusted_config_root=base_workspace.path,
                    trusted_source_root=base_workspace.path,
                    expected_source_locale=policy.source_locale,
                )
                outcome.prepared_value_edits += len(patch_result.changed_keys)

            commit_sha: str | None = None
            if (
                self.config.mode
                in {
                    GuardianMode.APPLY_OWNED_TRANSLATIONS,
                    GuardianMode.PROPOSE_PREVENTION,
                }
                and patch_result.changed_files
            ):
                commit_sha = self._publish_translation_commit(
                    policy=policy,
                    snapshot=snapshot,
                    workspace=head_workspace,
                    patch_result=patch_result,
                    assessments=assessments,
                    actionable=actionable,
                    translation_suppressed_feedback_ids=(
                        translation_suppressed_feedback_ids
                    ),
                    run_id=run_id,
                    lease_owner=lease_owner,
                )
                outcome.applied_commits.append(commit_sha)

            self._complete_actions(
                run_id=run_id,
                actionable=actionable,
                assessments=assessments,
                changed_keys=patch_result.changed_keys,
                commit_sha=commit_sha,
                translation_suppressed_feedback_ids=(
                    translation_suppressed_feedback_ids
                ),
                observed_at=observed_at,
            )
            self.state.finish_run(
                run_id,
                status="completed",
                summary="Guardian assessment completed within configured authority.",
                finished_at=observed_at,
            )
            outcome.runs_completed += 1
        except (_AuthenticationCircuit, _ModelCircuit):
            raise
        except PatchPolicyError:
            for revision in revisions:
                self.state.record_action(
                    run_id=run_id,
                    event_revision_id=revision.revision_id,
                    action=self.config.mode.value,
                    status="skipped",
                    details={"outcome": "deterministic_policy_rejection"},
                    occurred_at=observed_at,
                )
            self.state.finish_run(
                run_id,
                status="completed",
                summary="Model proposal was rejected by deterministic policy.",
                finished_at=observed_at,
            )
            outcome.runs_completed += 1
        except Exception as exc:
            self._fail_actions(
                run_id=run_id,
                revisions=revisions,
                outcome_name="orchestration_failure",
                observed_at=observed_at,
            )
            self.state.finish_run(
                run_id,
                status="failed",
                summary="Guardian action failed closed.",
                finished_at=observed_at,
            )
            outcome.runs_failed += 1
            raise exc

    @staticmethod
    def _record_prevention_outcome(
        result: PreventionBatchOutcome,
        *,
        outcome: _PollAccumulator,
    ) -> None:
        outcome.prevention_drafts_created += sum(
            1 for draft in result.drafts if draft.created
        )
        outcome.prevention_items_skipped += result.skipped
        outcome.prevention_items_deferred += result.deferred
        outcome.prevention_failures.extend(result.failures)

    def _require_live_lease(self, owner: str) -> None:
        if not self.state.refresh_lease(
            name="guardian:poll",
            owner=owner,
            ttl_seconds=_lease_ttl_seconds(self.config),
            now=_as_utc(self.now()),
        ):
            raise RuntimeError("Guardian poll lease was lost.")

    def _publish_translation_commit(
        self,
        *,
        policy: RepositoryPolicy,
        snapshot: PullRequestFeedbackSnapshot,
        workspace: GuardianWorkspace,
        patch_result: PatchResult,
        assessments: Sequence[GuardianAssessment],
        actionable: Sequence[tuple[FeedbackEvent, EventRevision]],
        translation_suppressed_feedback_ids: frozenset[str],
        run_id: str,
        lease_owner: str,
    ) -> str:
        if (
            self.write_broker_factory is None
        ):  # Constructor enforces this mode boundary.
            raise RuntimeError("Write broker is unavailable.")
        selected_feedback_ids = {
            assessment.feedback_id
            for assessment in assessments
            if assessment.verdict == "apply"
            and assessment.feedback_id
            not in translation_suppressed_feedback_ids
            and assessment.confidence >= self.config.limits.min_apply_confidence
            and assessment.replacements
        }
        selected = tuple(
            (event, revision)
            for event, revision in actionable
            if event.feedback_id in selected_feedback_ids
        )
        feedback_urls = tuple(
            dict.fromkeys(
                event.html_url
                for event, _revision in selected
                if event.html_url is not None
            )
        )
        commit = workspace.commit_validated_changes(
            expected_paths=patch_result.changed_files,
            pull_number=snapshot.pull_request.number,
            feedback_urls=feedback_urls,
            feedback_repository=policy.base_repo,
            sign=True,
            signing_key=self.signing_key,
            signing_environment=self.signing_environment,
        )
        publication_metadata = {
            "run_id": run_id,
            "repository": policy.base_repo,
            "pr_number": snapshot.pull_request.number,
            "original_head_sha": snapshot.pull_request.head_sha,
            "base_sha": snapshot.pull_request.base_sha,
            "commit_sha": commit.commit_sha,
            "event_revision_ids": tuple(
                revision.revision_id for _event, revision in selected
            ),
        }
        publication_key = self.state.record_publication_event(
            **publication_metadata,
            phase="prepared",
        )
        broker = self.write_broker_factory(policy)
        # This is deliberately adjacent to publication. The workspace then
        # performs its own normal-fetch exact-ref check before the non-force push.
        broker.verify_pull(
            pull_number=snapshot.pull_request.number,
            expected_head_sha=snapshot.pull_request.head_sha,
            expected_base_sha=snapshot.pull_request.base_sha,
        )
        def revalidate_before_push() -> None:
            self._require_live_lease(lease_owner)
            broker.verify_pull(
                pull_number=snapshot.pull_request.number,
                expected_head_sha=snapshot.pull_request.head_sha,
                expected_base_sha=snapshot.pull_request.base_sha,
            )

        publication = workspace.publish_commit(
            commit,
            credential_environment=self.publish_credential_environment,
            require_signature=True,
            signing_key=self.signing_key,
            signing_environment=self.signing_environment,
            before_push=revalidate_before_push,
        )
        self.state.record_publication_event(
            **publication_metadata,
            phase="published",
        )
        marker_revision = min(revision.revision_id for _event, revision in selected)
        self._require_live_lease(lease_owner)
        broker.post_commit_reply(
            pull_number=snapshot.pull_request.number,
            expected_head_sha=publication.commit_sha,
            expected_base_sha=snapshot.pull_request.base_sha,
            commit_sha=publication.commit_sha,
            action_id=publication_key,
            event_revision_id=str(marker_revision),
            before_create=lambda: self._require_live_lease(lease_owner),
        )
        self.state.record_publication_event(
            **publication_metadata,
            phase="replied",
        )
        return publication.commit_sha

    def _fail_actions(
        self,
        *,
        run_id: str,
        revisions: Sequence[EventRevision],
        outcome_name: str,
        observed_at: datetime,
    ) -> None:
        for revision in revisions:
            self.state.record_action(
                run_id=run_id,
                event_revision_id=revision.revision_id,
                action=self.config.mode.value,
                status="failed",
                details={"outcome": outcome_name},
                occurred_at=observed_at,
            )

    def _complete_actions(
        self,
        *,
        run_id: str,
        actionable: Sequence[tuple[FeedbackEvent, EventRevision]],
        assessments: Sequence[GuardianAssessment],
        changed_keys: Sequence[tuple[str, str]],
        commit_sha: str | None,
        translation_suppressed_feedback_ids: frozenset[str],
        observed_at: datetime,
    ) -> None:
        assessments_by_id = {
            assessment.feedback_id: assessment for assessment in assessments
        }
        changed_key_set = set(changed_keys)
        for event, revision in actionable:
            assessment = assessments_by_id[event.feedback_id]
            eligible = (
                event.feedback_id not in translation_suppressed_feedback_ids
                and assessment.verdict == "apply"
                and assessment.confidence >= self.config.limits.min_apply_confidence
                and bool(assessment.replacements)
            )
            assessment_keys = {
                (replacement.path, replacement.key)
                for replacement in assessment.replacements
            }
            applied_keys = changed_key_set & assessment_keys if eligible else set()
            self.state.record_action(
                run_id=run_id,
                event_revision_id=revision.revision_id,
                action=self.config.mode.value,
                status="completed",
                details={
                    "outcome": (
                        "applied"
                        if commit_sha is not None and applied_keys
                        else "prepared"
                        if applied_keys
                        else "would_apply"
                        if self.config.mode is GuardianMode.OBSERVE and eligible
                        else "prevention_assessed_after_translation"
                        if event.feedback_id
                        in translation_suppressed_feedback_ids
                        else "no_eligible_change"
                    ),
                    "verdict": assessment.verdict,
                    "confidence": assessment.confidence,
                    "changed_keys": len(applied_keys),
                    "commit_sha": commit_sha if applied_keys else None,
                    "recurrence_candidates": len(assessment.recurrence_candidates),
                },
                occurred_at=observed_at,
            )


def run_once(*, config_path: Path, scheduled: bool = False) -> int:
    """Lazily enter the production runtime without creating an import cycle."""

    from localize.guardian.runtime import run_once as runtime_run_once

    return runtime_run_once(config_path=config_path, scheduled=scheduled)


__all__ = (
    "CheckoutFactory",
    "CodexRunner",
    "GuardianController",
    "PollOutcome",
    "PreventionRunner",
    "SnapshotProvider",
    "WriteBrokerFactory",
    "run_once",
)
