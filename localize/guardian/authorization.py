"""Deterministically authorize GitHub feedback before any model sees it."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import re
from typing import Mapping, Sequence

from localize.guardian.github import (
    FeedbackRevision,
    PullRequestFeedbackSnapshot,
)
from localize.guardian.models import FeedbackEvent, RepositoryPolicy


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_GUARDIAN_MARKER = "<!-- localize-guardian:v1 "


class IntakePolicyError(ValueError):
    """Raised when a PR snapshot is outside the operator's trusted boundary."""


@dataclass(frozen=True)
class SkippedFeedback:
    """Redacted metadata explaining why one feedback object was rejected."""

    kind: str
    source_id: str
    revision_id: str
    reason: str


@dataclass(frozen=True)
class AuthorizedFeedback:
    """Authorized events plus explicit reasons for every rejected object."""

    events: tuple[FeedbackEvent, ...]
    skipped: tuple[SkippedFeedback, ...]


def _matches_actor(
    policy: RepositoryPolicy,
    *,
    locale: str,
    actor_id: int,
    actor_type: str,
) -> bool:
    reviewer = policy.trusted_reviewer_by_id(locale, actor_id)
    bot = policy.trusted_bot_by_id(locale, actor_id)
    return bool(
        (reviewer is not None and reviewer.type == actor_type)
        or (bot is not None and bot.type == actor_type)
    )


def _actor_locales(
    policy: RepositoryPolicy,
    revision: FeedbackRevision,
    *,
    changed_locales: set[str],
) -> tuple[str, ...]:
    if revision.author_id is None:
        return ()
    configured = set(policy.trusted_reviewers) | set(policy.trusted_bots)
    return tuple(
        sorted(
            locale
            for locale in configured & changed_locales
            if _matches_actor(
                policy,
                locale=locale,
                actor_id=revision.author_id,
                actor_type=revision.author_type,
            )
        )
    )


def _authorize_pull(
    policy: RepositoryPolicy,
    snapshot: PullRequestFeedbackSnapshot,
) -> None:
    identity = snapshot.repository_identity
    pull = snapshot.pull_request
    if (
        identity.full_name != policy.base_repo
        or identity.repository_id != policy.base_repo_id
        or pull.repository != policy.base_repo
        or pull.base_repository_id != policy.base_repo_id
    ):
        raise IntakePolicyError("GitHub repository identity does not match Guardian policy.")
    if identity.private and not policy.private_repo_model_opt_in:
        raise IntakePolicyError(
            "Guardian model access to this private repository is not explicitly enabled."
        )
    author = (
        policy.allowed_pr_author_by_id(pull.author_id)
        if pull.author_id is not None
        else None
    )
    head_owner = (
        policy.allowed_head_owner_by_id(pull.head_owner_id)
        if pull.head_owner_id is not None
        else None
    )
    head_repository = (
        policy.allowed_head_repository_by_id(pull.head_repository_id)
        if pull.head_repository_id is not None
        else None
    )
    if (
        pull.state.casefold() != "open"
        or pull.base_ref != policy.base_branch
        or author is None
        or author.type != pull.author_type
        or head_owner is None
        or head_owner.type != pull.head_owner_type
        or head_repository is None
        or not any(fnmatchcase(pull.head_ref, glob) for glob in policy.allowed_branch_globs)
        or not _FULL_SHA.fullmatch(pull.head_sha)
        or not _FULL_SHA.fullmatch(pull.base_sha)
    ):
        raise IntakePolicyError("Pull request does not match the owned pull-request policy.")


def _skip(revision: FeedbackRevision, reason: str) -> SkippedFeedback:
    return SkippedFeedback(
        kind=revision.kind.value,
        source_id=revision.source_id,
        revision_id=revision.revision_id,
        reason=reason,
    )


def authorize_feedback(
    *,
    policy: RepositoryPolicy,
    snapshot: PullRequestFeedbackSnapshot,
    path_locales: Mapping[str, str],
    changed_locales: Sequence[str],
) -> AuthorizedFeedback:
    """Bind visible GitHub feedback to a trusted actor and target locale.

    Login names are retained only for display. Numeric actor, repository, and
    pull-request identities plus actor types are the authorization boundary.
    """

    _authorize_pull(policy, snapshot)
    locale_scope = set(changed_locales)
    if not locale_scope:
        raise IntakePolicyError("Owned pull request has no changed target locales.")

    events: list[FeedbackEvent] = []
    skipped: list[SkippedFeedback] = []
    seen_ids: set[str] = set()
    pull = snapshot.pull_request
    for revision in snapshot.feedback:
        stable_id = f"{revision.kind.value}:{revision.source_id}"
        if stable_id in seen_ids:
            raise IntakePolicyError(f"GitHub snapshot repeats feedback object {stable_id!r}.")
        seen_ids.add(stable_id)
        if (
            revision.repository != policy.base_repo
            or revision.pull_number != pull.number
        ):
            raise IntakePolicyError("Feedback object does not belong to the authorized pull request.")
        if revision.deleted:
            skipped.append(_skip(revision, "deleted"))
            continue
        if not revision.body.strip():
            skipped.append(_skip(revision, "blank"))
            continue
        if _GUARDIAN_MARKER in revision.body:
            skipped.append(_skip(revision, "guardian_generated"))
            continue
        if revision.author_id is None:
            skipped.append(_skip(revision, "untrusted_actor"))
            continue

        actor_locales = _actor_locales(
            policy,
            revision,
            changed_locales=locale_scope,
        )
        if revision.path is not None:
            locale = path_locales.get(revision.path)
            if locale is None:
                skipped.append(_skip(revision, "unrecognized_path"))
                continue
            if locale not in actor_locales:
                skipped.append(_skip(revision, "untrusted_actor"))
                continue
        else:
            if not actor_locales:
                skipped.append(_skip(revision, "untrusted_actor"))
                continue
            if len(actor_locales) != 1:
                skipped.append(_skip(revision, "ambiguous_locale"))
                continue
            locale = actor_locales[0]

        events.append(
            FeedbackEvent(
                repository=policy.base_repo,
                pr_number=pull.number,
                kind=revision.kind.value,
                event_id=revision.source_id,
                author=revision.author_login,
                author_id=revision.author_id,
                author_type=revision.author_type,
                body=revision.body,
                head_sha=pull.head_sha,
                base_sha=pull.base_sha,
                locale=locale,
                updated_at=revision.updated_at,
                path=revision.path,
                line=revision.line,
                html_url=revision.html_url,
                deleted=False,
            )
        )

    return AuthorizedFeedback(events=tuple(events), skipped=tuple(skipped))


__all__ = (
    "AuthorizedFeedback",
    "IntakePolicyError",
    "SkippedFeedback",
    "authorize_feedback",
)
