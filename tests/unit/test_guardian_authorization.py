from dataclasses import replace

import pytest

from localize.guardian.authorization import IntakePolicyError, authorize_feedback
from localize.guardian.github import (
    CodeRabbitCoverage,
    CodeRabbitCoverageStatus,
    FeedbackKind,
    FeedbackRevision,
    GitHubRepositoryIdentity,
    PullRequestFeedbackSnapshot,
    PullRequestSnapshot,
)
from localize.guardian.models import (
    AllowedHeadRepository,
    RepositoryPolicy,
    TrustedActor,
)


def _policy(*, private_opt_in=False):
    return RepositoryPolicy(
        base_repo="acme/widgets",
        base_repo_id=42,
        base_branch="main",
        allowed_pr_authors=(TrustedActor("translation-service", 8, "Bot"),),
        allowed_head_owners=(TrustedActor("contributor", 7, "User"),),
        allowed_head_repositories=(
            AllowedHeadRepository("contributor/widgets", 84),
        ),
        allowed_branch_globs=("localize/*",),
        allowed_path_globs=("l10n/**",),
        pipeline_config_path=".localize/config.yaml",
        source_locale="en",
        trusted_reviewers={
            "ru": (TrustedActor("ru-reviewer", 101, "User"),),
            "de": (TrustedActor("de-reviewer", 102, "User"),),
        },
        trusted_bots={
            "ru": (TrustedActor("review-bot[bot]", 202, "Bot"),),
        },
        private_repo_model_opt_in=private_opt_in,
    )


def _pull(**overrides):
    values = dict(
        repository="acme/widgets",
        base_repository_id=42,
        pull_id=500,
        number=12,
        state="open",
        html_url="https://github.test/acme/widgets/pull/12",
        created_at="2026-08-30T08:00:00Z",
        updated_at="2026-08-30T10:00:00Z",
        author_login="renamed-translation-service",
        author_id=8,
        author_type="Bot",
        head_sha="a" * 40,
        head_ref="localize/russian",
        head_owner="renamed-contributor",
        head_owner_id=7,
        head_owner_type="User",
        head_repository="renamed-contributor/widgets",
        head_repository_id=84,
        base_sha="b" * 40,
        base_ref="main",
    )
    values.update(overrides)
    return PullRequestSnapshot(**values)


def _feedback(item_id, **overrides):
    values = dict(
        repository="acme/widgets",
        pull_number=12,
        kind=FeedbackKind.REVIEW_COMMENT,
        source_id=str(item_id),
        node_id=f"node-{item_id}",
        author_login="renamed-ru-reviewer",
        author_id=101,
        author_type="User",
        body="Prefer this native wording.",
        created_at="2026-08-30T09:00:00Z",
        updated_at="2026-08-30T10:00:00Z",
        html_url=f"https://github.test/review/{item_id}",
        path="l10n/messages_ru.properties",
        line=17,
    )
    values.update(overrides)
    return FeedbackRevision(**values)


def _snapshot(*feedback, private=False, pull=None):
    return PullRequestFeedbackSnapshot(
        repository_identity=GitHubRepositoryIdentity(
            full_name="acme/widgets",
            repository_id=42,
            private=private,
        ),
        pull_request=pull or _pull(),
        feedback=tuple(feedback),
        coderabbit=CodeRabbitCoverage(CodeRabbitCoverageStatus.REVIEWED),
    )


def test_authorizes_human_and_explicit_bot_by_numeric_id_and_locale():
    result = authorize_feedback(
        policy=_policy(),
        snapshot=_snapshot(
            _feedback(1),
            _feedback(
                2,
                author_login="renamed-bot[bot]",
                author_id=202,
                author_type="Bot",
                kind=FeedbackKind.ISSUE_COMMENT,
                path=None,
            ),
        ),
        path_locales={"l10n/messages_ru.properties": "ru"},
        changed_locales=("ru",),
    )

    assert [event.feedback_id for event in result.events] == [
        "review_comment:1",
        "issue_comment:2",
    ]
    assert all(event.locale == "ru" for event in result.events)
    assert result.events[0].path == "l10n/messages_ru.properties"
    assert result.events[0].line == 17
    assert result.events[0].author == "renamed-ru-reviewer"
    assert result.skipped == ()


def test_rejects_login_spoof_wrong_type_and_ambiguous_locale_as_audited_skips():
    result = authorize_feedback(
        policy=_policy(),
        snapshot=_snapshot(
            _feedback(1, author_login="ru-reviewer", author_id=999),
            _feedback(2, author_id=101, author_type="Bot"),
            _feedback(
                3,
                path="src/Unrelated.java",
                author_id=101,
            ),
        ),
        path_locales={"l10n/messages_ru.properties": "ru"},
        changed_locales=("ru", "de"),
    )

    assert result.events == ()
    assert [item.reason for item in result.skipped] == [
        "untrusted_actor",
        "untrusted_actor",
        "unrecognized_path",
    ]


def test_issue_comment_is_ambiguous_when_actor_is_whitelisted_for_two_changed_locales():
    policy = _policy()
    policy = replace(
        policy,
        trusted_reviewers={
            **policy.trusted_reviewers,
            "de": (TrustedActor("same-human", 101, "User"),),
        },
    )
    result = authorize_feedback(
        policy=policy,
        snapshot=_snapshot(_feedback(9, kind=FeedbackKind.ISSUE_COMMENT, path=None)),
        path_locales={},
        changed_locales=("ru", "de"),
    )

    assert result.events == ()
    assert result.skipped[0].reason == "ambiguous_locale"


def test_excludes_deleted_blank_and_guardian_generated_feedback():
    result = authorize_feedback(
        policy=_policy(),
        snapshot=_snapshot(
            _feedback(1, deleted=True),
            _feedback(2, body=""),
            _feedback(
                3,
                body="<!-- localize-guardian:v1 action=x event=y -->\nBot status",
            ),
        ),
        path_locales={"l10n/messages_ru.properties": "ru"},
        changed_locales=("ru",),
    )

    assert result.events == ()
    assert [item.reason for item in result.skipped] == [
        "deleted",
        "blank",
        "guardian_generated",
    ]


@pytest.mark.parametrize(
    "pull",
    [
        _pull(author_id=999),
        _pull(author_type="User"),
        _pull(head_owner_id=999),
        _pull(head_owner_type="Bot"),
        _pull(head_repository_id=999),
        _pull(head_ref="untrusted/branch"),
        _pull(base_repository_id=999),
        _pull(base_ref="release"),
        _pull(state="closed"),
    ],
)
def test_rejects_pull_requests_outside_owned_branch_policy(pull):
    with pytest.raises(IntakePolicyError, match="policy"):
        authorize_feedback(
            policy=_policy(),
            snapshot=_snapshot(_feedback(1), pull=pull),
            path_locales={"l10n/messages_ru.properties": "ru"},
            changed_locales=("ru",),
        )


def test_private_repository_requires_explicit_model_opt_in():
    with pytest.raises(IntakePolicyError, match="private repository"):
        authorize_feedback(
            policy=_policy(),
            snapshot=_snapshot(_feedback(1), private=True),
            path_locales={"l10n/messages_ru.properties": "ru"},
            changed_locales=("ru",),
        )

    accepted = authorize_feedback(
        policy=_policy(private_opt_in=True),
        snapshot=_snapshot(_feedback(1), private=True),
        path_locales={"l10n/messages_ru.properties": "ru"},
        changed_locales=("ru",),
    )
    assert len(accepted.events) == 1


def test_rejects_snapshot_identity_mismatch_before_inspecting_feedback():
    snapshot = replace(
        _snapshot(_feedback(1)),
        repository_identity=GitHubRepositoryIdentity(
            full_name="attacker/widgets",
            repository_id=42,
            private=False,
        ),
    )
    with pytest.raises(IntakePolicyError, match="repository identity"):
        authorize_feedback(
            policy=_policy(),
            snapshot=snapshot,
            path_locales={"l10n/messages_ru.properties": "ru"},
            changed_locales=("ru",),
        )
