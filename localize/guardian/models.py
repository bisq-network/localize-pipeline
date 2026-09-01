"""Typed data exchanged by the localization PR guardian."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from pathlib import Path
from typing import Mapping


class GuardianMode(str, Enum):
    """The maximum authority granted to one guardian run."""

    OBSERVE = "observe"
    PREPARE = "prepare"
    APPLY_OWNED_TRANSLATIONS = "apply-owned-translations"
    PROPOSE_PREVENTION = "propose-prevention"


class CodexAuthMode(str, Enum):
    """How Guardian authenticates the local Codex CLI."""

    CHATGPT = "chatgpt"
    API_KEY = "api-key"


class PipelineConfigSource(str, Enum):
    """Trusted origin for one repository's localization pipeline policy."""

    BASE = "base"
    OPERATOR = "operator"


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _validate_repository_name(value: str, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not _REPOSITORY_RE.fullmatch(value)
        or any(component in {".", ".."} for component in value.split("/"))
    ):
        raise ValueError(f"{field_name} must use canonical owner/name form.")


@dataclass(frozen=True)
class GuardianLimits:
    """Resource and change limits applied independently to every run."""

    run_timeout_seconds: int = 3600
    max_attempts: int = 2
    max_value_edits_per_run: int = 20
    max_prevention_drafts_per_run: int = 1
    max_model_calls_per_day: int = 2
    daily_cost_limit_usd: float | None = None
    model_call_reservation_usd: float | None = None
    min_apply_confidence: float = 0.9
    raw_retention_days: int = 90


@dataclass(frozen=True)
class GuardianSchedule:
    """Once-daily local wall-clock schedule used by scheduled invocations."""

    hour: int = 0
    minute: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.hour, bool) or not isinstance(self.hour, int):
            raise ValueError("Schedule hour must be an integer.")
        if isinstance(self.minute, bool) or not isinstance(self.minute, int):
            raise ValueError("Schedule minute must be an integer.")
        if not 0 <= self.hour <= 23:
            raise ValueError("Schedule hour must be between 0 and 23.")
        if not 0 <= self.minute <= 59:
            raise ValueError("Schedule minute must be between 0 and 59.")


@dataclass(frozen=True)
class GuardianRuntime:
    """Secret-free local executables and credential-broker commands."""

    codex_model: str = "gpt-5.6-terra"
    codex_reasoning_effort: str = "high"
    codex_auth_mode: CodexAuthMode = CodexAuthMode.CHATGPT
    codex_home: str = "~/.local/share/localize-guardian/codex"
    codex_executable: str = "codex"
    git_executable: str = "git"
    signing_program: str = "gpg"
    github_token_command: tuple[str, ...] = ("gh", "auth", "token")
    codex_api_key_command: tuple[str, ...] = ()
    signing_key: str | None = None


@dataclass(frozen=True)
class TrustedActor:
    """A GitHub actor pinned by immutable numeric identity."""

    login: str
    id: int
    type: str

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("Trusted actor id must be positive.")
        if self.type not in {"User", "Bot", "Organization"}:
            raise ValueError("Trusted actor type must be User, Bot, or Organization.")


@dataclass(frozen=True)
class AllowedHeadRepository:
    """One exact GitHub repository in which Guardian may advance a PR branch."""

    full_name: str
    id: int

    def __post_init__(self) -> None:
        _validate_repository_name(self.full_name, field_name="full_name")
        if self.id <= 0:
            raise ValueError("Allowed head repository id must be positive.")


@dataclass(frozen=True)
class ExactRepository:
    """A GitHub repository pinned by full name and immutable numeric ID."""

    full_name: str
    id: int

    def __post_init__(self) -> None:
        _validate_repository_name(self.full_name, field_name="full_name")
        if self.id <= 0:
            raise ValueError("Exact repository id must be positive.")


@dataclass(frozen=True)
class PreventionPolicy:
    """Explicit authority for preparing one bounded prevention draft."""

    target_repository: ExactRepository
    target_base_branch: str
    push_repository: ExactRepository
    push_branch_prefix: str
    allowed_code_path_globs: tuple[str, ...]
    allowed_test_path_globs: tuple[str, ...]
    focused_test_argv: tuple[tuple[str, ...], ...]
    sandbox_argv_prefix: tuple[str, ...]
    max_changed_files: int
    max_changed_bytes: int
    private_target_model_opt_in: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_code_path_globs",
            tuple(self.allowed_code_path_globs),
        )
        object.__setattr__(
            self,
            "allowed_test_path_globs",
            tuple(self.allowed_test_path_globs),
        )
        object.__setattr__(
            self,
            "focused_test_argv",
            tuple(tuple(argv) for argv in self.focused_test_argv),
        )
        object.__setattr__(
            self,
            "sandbox_argv_prefix",
            tuple(self.sandbox_argv_prefix),
        )
        if self.max_changed_files <= 0:
            raise ValueError("max_changed_files must be positive.")
        if self.max_changed_bytes <= 0:
            raise ValueError("max_changed_bytes must be positive.")
        if not isinstance(self.private_target_model_opt_in, bool):
            raise ValueError("private_target_model_opt_in must be a boolean.")


@dataclass(frozen=True)
class RepositoryPolicy:
    """Least-privilege policy for one base repository."""

    base_repo: str
    base_repo_id: int
    base_branch: str
    allowed_pr_authors: tuple[TrustedActor, ...]
    allowed_head_owners: tuple[TrustedActor, ...]
    allowed_head_repositories: tuple[AllowedHeadRepository, ...]
    allowed_branch_globs: tuple[str, ...]
    allowed_path_globs: tuple[str, ...]
    pipeline_config_path: str
    source_locale: str
    trusted_reviewers: Mapping[str, tuple[TrustedActor, ...]]
    trusted_bots: Mapping[str, tuple[TrustedActor, ...]]
    private_repo_model_opt_in: bool = False
    prevention: PreventionPolicy | None = None
    pipeline_config_source: PipelineConfigSource = PipelineConfigSource.BASE

    def __post_init__(self) -> None:
        _validate_repository_name(self.base_repo, field_name="base_repo")
        if self.base_repo_id <= 0:
            raise ValueError("Base repository id must be positive.")
        object.__setattr__(self, "allowed_pr_authors", tuple(self.allowed_pr_authors))
        object.__setattr__(self, "allowed_head_owners", tuple(self.allowed_head_owners))
        object.__setattr__(
            self,
            "allowed_head_repositories",
            tuple(self.allowed_head_repositories),
        )
        reviewers = {
            locale: tuple(accounts)
            for locale, accounts in self.trusted_reviewers.items()
        }
        bots = {
            locale: tuple(accounts)
            for locale, accounts in self.trusted_bots.items()
        }
        object.__setattr__(self, "trusted_reviewers", MappingProxyType(reviewers))
        object.__setattr__(self, "trusted_bots", MappingProxyType(bots))
        object.__setattr__(
            self,
            "pipeline_config_source",
            PipelineConfigSource(self.pipeline_config_source),
        )

    def trusted_reviewers_for(self, locale: str) -> tuple[TrustedActor, ...]:
        """Return only the reviewers trusted for this repository and locale."""

        return self.trusted_reviewers.get(locale, ())

    def trusted_bots_for(self, locale: str) -> tuple[TrustedActor, ...]:
        """Return bots trusted only as deterministic feedback sources."""

        return self.trusted_bots.get(locale, ())

    def trusted_reviewer_by_id(
        self,
        locale: str,
        actor_id: int,
    ) -> TrustedActor | None:
        """Look up native-human authority by immutable ID, never by login."""

        return next(
            (actor for actor in self.trusted_reviewers_for(locale) if actor.id == actor_id),
            None,
        )

    def trusted_bot_by_id(
        self,
        locale: str,
        actor_id: int,
    ) -> TrustedActor | None:
        """Look up deterministic-bot authority by immutable ID."""

        return next(
            (actor for actor in self.trusted_bots_for(locale) if actor.id == actor_id),
            None,
        )

    def allowed_pr_author_by_id(self, actor_id: int) -> TrustedActor | None:
        """Look up an allowed PR author by immutable GitHub actor ID."""

        return next(
            (actor for actor in self.allowed_pr_authors if actor.id == actor_id),
            None,
        )

    def allowed_head_owner_by_id(self, actor_id: int) -> TrustedActor | None:
        """Look up an allowed owned-branch repository owner by immutable ID."""

        return next(
            (actor for actor in self.allowed_head_owners if actor.id == actor_id),
            None,
        )

    def allowed_head_repository_by_id(
        self,
        repository_id: int,
    ) -> AllowedHeadRepository | None:
        """Look up an exact writable head repository by immutable ID."""

        return next(
            (
                repository
                for repository in self.allowed_head_repositories
                if repository.id == repository_id
            ),
            None,
        )


@dataclass(frozen=True)
class GuardianConfig:
    """Validated guardian configuration."""

    repositories: tuple[RepositoryPolicy, ...]
    mode: GuardianMode = GuardianMode.OBSERVE
    limits: GuardianLimits = field(default_factory=GuardianLimits)
    runtime: GuardianRuntime = field(default_factory=GuardianRuntime)
    schedule: GuardianSchedule = field(default_factory=GuardianSchedule)

    @property
    def report_only(self) -> bool:
        """Whether this configuration forbids preparing or applying changes."""

        return self.mode is GuardianMode.OBSERVE


@dataclass(frozen=True)
class PipelineConfigSnapshot:
    """Immutable private copy of one operator-owned pipeline config bundle."""

    config_root: Path
    config_path: Path
    bundle_digest: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.bundle_digest):
            raise ValueError("Pipeline config bundle digest must be SHA-256 hex.")


@dataclass(frozen=True)
class FeedbackEvent:
    """One immutable snapshot of review feedback at exact PR revisions."""

    repository: str
    pr_number: int
    kind: str
    event_id: str
    author: str
    author_id: int
    author_type: str
    body: str
    head_sha: str
    base_sha: str
    locale: str
    updated_at: str | None = None
    path: str | None = None
    line: int | None = None
    html_url: str | None = None
    deleted: bool = False

    @property
    def feedback_id(self) -> str:
        """Return the stable identifier used in assessments and replacements."""

        return f"{self.kind}:{self.event_id}"


@dataclass(frozen=True)
class ProposedReplacement:
    """A value-only localization replacement proposed for one feedback item."""

    feedback_id: str
    path: str
    key: str
    locale: str
    expected_value: str
    proposed_value: str
    confidence: float
    evidence: tuple[str, ...]
    source_value: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1.")
        evidence = (self.evidence,) if isinstance(self.evidence, str) else tuple(self.evidence)
        object.__setattr__(self, "evidence", evidence)


@dataclass(frozen=True)
class RecurrenceCandidate:
    """A possible durable prevention change supported by feedback evidence."""

    scope: str
    summary: str
    evidence_feedback_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        evidence = self.evidence_feedback_ids
        if isinstance(evidence, str):
            evidence = (evidence,)
        object.__setattr__(
            self,
            "evidence_feedback_ids",
            tuple(evidence),
        )


@dataclass(frozen=True)
class GuardianAssessment:
    """Structured model assessment for a single feedback item."""

    feedback_id: str
    verdict: str
    confidence: float
    rationale: str
    replacements: tuple[ProposedReplacement, ...] = ()
    recurrence_candidates: tuple[RecurrenceCandidate, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict not in {"apply", "reject", "needs_human"}:
            raise ValueError("verdict must be apply, reject, or needs_human.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1.")
        object.__setattr__(self, "replacements", tuple(self.replacements))
        object.__setattr__(
            self,
            "recurrence_candidates",
            tuple(self.recurrence_candidates),
        )
