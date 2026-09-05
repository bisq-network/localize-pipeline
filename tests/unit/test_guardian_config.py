"""Tests for the PR guardian's strict, least-privilege configuration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from localize.guardian import (
    AllowedHeadRepository,
    CodexAuthMode,
    ExactRepository,
    GuardianMode,
    PipelineConfigSource,
    PreventionPolicy,
    ProposedReplacement,
    RepositoryPolicy,
    SigningFormat,
    TrustedActor,
)
from localize.guardian.config import (
    GuardianConfigError,
    load_guardian_config,
    parse_guardian_config,
)
from localize.guardian.models import (
    ClosedPrBackfillPolicy,
    GuardianConfig,
    GuardianLimits,
    GuardianRuntime,
    HistoricalRemediationPolicy,
    pipeline_config_bundle_digest,
)


def _write_config(tmp_path: Path, yaml_text: str) -> Path:
    path = tmp_path / "guardian.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def _minimal_config() -> str:
    return """
repositories:
  - base_repo: acme/widgets
    base_repo_id: 42
    base_branch: main
    publication_actor: {login: localize-bot, id: 11, type: User}
    allowed_pr_authors:
      - {login: localize-bot, id: 11, type: User}
    allowed_head_owners:
      - {login: acme, id: 12, type: Organization}
    allowed_head_repositories:
      - {full_name: localize-bot/widgets, id: 84}
    allowed_branch_globs: ["localization/**"]
    allowed_path_globs: ["src/main/resources/l10n/**"]
    pipeline_config_path: .localize/config.yaml
    source_locale: en
    trusted_reviewers:
      ru:
        - {login: locale-maintainer, id: 101, type: User}
    trusted_bots:
      ru:
        - {login: "coderabbitai[bot]", id: 202, type: Bot}
"""


def _prevention_policy_yaml() -> str:
    return """    prevention:
      target_repository:
        full_name: acme/localization-pipeline
        id: 501
        base_branch: main
      push_repository:
        full_name: localize-bot/localization-pipeline
        id: 502
        branch_prefix: guardian/prevention-
      publication_actor:
        login: localize-bot
        id: 11
        type: User
      allowed_code_path_globs:
        - "localize/**/*.py"
      allowed_test_path_globs:
        - "tests/**/*.py"
      focused_test_argv:
        - [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]
        - [/opt/localize-guardian/bin/ruff, check, localize, tests]
      sandbox_argv_prefix:
        - /opt/localize-guardian/bin/sandbox-wrapper
      max_changed_files: 4
      max_changed_bytes: 262144
      private_target_model_opt_in: false
"""


def _config_with_prevention() -> str:
    return _minimal_config().replace(
        "    trusted_reviewers:\n",
        _prevention_policy_yaml() + "    trusted_reviewers:\n",
        1,
    )


def _raw_config_with_prevention() -> dict:
    raw = yaml.safe_load(_config_with_prevention())
    assert isinstance(raw, dict)
    return raw


def _allow_remediation_namespace(config_text: str) -> str:
    return config_text.replace(
        '    allowed_branch_globs: ["localization/**"]\n',
        '    allowed_branch_globs: ["localization/**", '
        '"localization/guardian-remediation-*"]\n',
        1,
    )


def test_minimal_config_is_report_only_with_safe_limits(tmp_path: Path) -> None:
    config = load_guardian_config(_write_config(tmp_path, _minimal_config()))

    assert config.mode is GuardianMode.OBSERVE
    assert config.report_only is True
    assert config.limits.run_timeout_seconds == 3600
    assert config.limits.max_attempts == 2
    assert config.limits.max_value_edits_per_run == 20
    assert config.limits.max_prevention_drafts_per_run == 1
    assert config.limits.max_remediation_drafts_per_run == 0
    assert config.limits.max_model_calls_per_day == 2
    assert config.limits.daily_cost_limit_usd is None
    assert config.limits.model_call_reservation_usd is None
    assert config.limits.min_apply_confidence == 0.9
    assert config.limits.raw_retention_days == 90
    assert config.runtime.codex_model == "gpt-5.6-terra"
    assert config.runtime.codex_reasoning_effort == "high"
    assert config.runtime.codex_auth_mode is CodexAuthMode.CHATGPT
    assert config.runtime.codex_home == "~/.local/share/localize-guardian/codex"
    assert config.runtime.codex_executable == "codex"
    assert config.runtime.git_executable == "git"
    assert config.runtime.signing_format is SigningFormat.OPENPGP
    assert config.runtime.signing_program == "gpg"
    assert config.runtime.github_token_command == ("gh", "auth", "token")
    assert config.runtime.codex_api_key_command == ()
    assert config.runtime.signing_key is None
    assert config.runtime.signing_public_key is None
    assert config.schedule.hour == 0
    assert config.schedule.minute == 0

    policy = config.repositories[0]
    assert policy.base_repo == "acme/widgets"
    assert policy.base_repo_id == 42
    assert policy.base_branch == "main"
    assert policy.publication_actor == TrustedActor(
        login="localize-bot", id=11, type="User"
    )
    assert policy.allowed_pr_author_by_id(11) == TrustedActor(
        login="localize-bot", id=11, type="User"
    )
    assert policy.allowed_head_owner_by_id(12) == TrustedActor(
        login="acme", id=12, type="Organization"
    )
    assert policy.allowed_head_repository_by_id(84).full_name == "localize-bot/widgets"
    assert policy.private_repo_model_opt_in is False
    assert policy.pipeline_config_source is PipelineConfigSource.BASE
    assert policy.prevention is None
    assert policy.closed_pr_backfill is None
    assert policy.trusted_reviewers_for("ru") == (
        TrustedActor(login="locale-maintainer", id=101, type="User"),
    )
    assert policy.trusted_bots_for("ru") == (
        TrustedActor(login="coderabbitai[bot]", id=202, type="Bot"),
    )
    assert policy.trusted_reviewers_for("de") == ()
    assert policy.trusted_bots_for("de") == ()


def test_observe_and_prepare_allow_omitting_publication_actor(tmp_path: Path) -> None:
    without_actor = _minimal_config().replace(
        "    publication_actor: {login: localize-bot, id: 11, type: User}\n",
        "",
        1,
    )

    for mode in ("observe", "prepare"):
        config = load_guardian_config(
            _write_config(tmp_path, f"mode: {mode}\n{without_actor}")
        )
        assert config.repositories[0].publication_actor is None
        assert config.enabled_publication_actors == ()


@pytest.mark.parametrize(
    "mode",
    ("apply-owned-translations", "propose-prevention"),
)
def test_write_modes_require_repository_publication_actor(
    tmp_path: Path,
    mode: str,
) -> None:
    config_text = (
        _config_with_prevention()
        if mode == "propose-prevention"
        else _minimal_config()
    ).replace(
        "    publication_actor: {login: localize-bot, id: 11, type: User}\n",
        "",
        1,
    )

    with pytest.raises(GuardianConfigError, match="publication_actor"):
        load_guardian_config(
            _write_config(
                tmp_path,
                f"mode: {mode}\nlimits:\n  max_model_calls_per_day: 4\n"
                f"{config_text}",
            )
        )


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("id: 11", "id: true"),
        ("type: User", "type: Bot"),
        ("login: localize-bot", 'login: "localize-bot\\nforged"'),
    ),
)
def test_repository_publication_actor_is_one_typed_bounded_identity(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    malformed = _minimal_config().replace(original, replacement, 1)

    with pytest.raises(GuardianConfigError, match="publication_actor"):
        load_guardian_config(_write_config(tmp_path, malformed))


def test_publication_actor_does_not_broaden_allowed_pr_authors(
    tmp_path: Path,
) -> None:
    config_text = _minimal_config().replace(
        "publication_actor: {login: localize-bot, id: 11, type: User}",
        "publication_actor: {login: maintainer, id: 99, type: User}",
        1,
    )

    config = load_guardian_config(
        _write_config(tmp_path, f"mode: apply-owned-translations\n{config_text}")
    )

    policy = config.repositories[0]
    assert policy.publication_actor == TrustedActor("maintainer", 99, "User")
    assert policy.allowed_pr_author_by_id(99) is None


@pytest.mark.parametrize("actor_type", ["Bot", "Organization"])
def test_direct_repository_publication_actor_rejects_non_user(
    tmp_path: Path,
    actor_type: str,
) -> None:
    policy = load_guardian_config(
        _write_config(tmp_path, _minimal_config())
    ).repositories[0]

    with pytest.raises(ValueError, match="publication_actor"):
        replace(
            policy,
            publication_actor=TrustedActor("acme", 12, actor_type),
        )


def test_repository_policy_preserves_legacy_positional_defaults() -> None:
    prevention = PreventionPolicy(
        target_repository=ExactRepository("acme/pipeline", 501),
        target_base_branch="main",
        push_repository=ExactRepository("localize-bot/pipeline", 502),
        push_branch_prefix="guardian/prevention-",
        publication_actor=TrustedActor("localize-bot", 11, "User"),
        allowed_code_path_globs=("localize/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(("/opt/bin/pytest", "tests/unit/test_rules.py"),),
        sandbox_argv_prefix=("/usr/bin/guardian-sandbox-wrapper",),
        max_changed_files=4,
        max_changed_bytes=262_144,
    )

    policy = RepositoryPolicy(
        "acme/widgets",
        42,
        "main",
        (TrustedActor("localize-bot", 11, "User"),),
        (TrustedActor("acme", 12, "Organization"),),
        (AllowedHeadRepository("localize-bot/widgets", 84),),
        ("localization/**",),
        ("l10n/**",),
        "config.yaml",
        "en",
        {"ru": (TrustedActor("reviewer", 101, "User"),)},
        {},
        True,
        prevention,
    )

    assert policy.private_repo_model_opt_in is True
    assert policy.prevention is prevention
    assert policy.pipeline_config_source is PipelineConfigSource.BASE
    assert policy.closed_pr_backfill is None


@pytest.mark.parametrize("actor_type", ["Bot", "Organization"])
def test_prevention_publication_actor_must_be_a_user(actor_type: str) -> None:
    with pytest.raises(ValueError, match="User identity"):
        PreventionPolicy(
            target_repository=ExactRepository("acme/pipeline", 501),
            target_base_branch="main",
            push_repository=ExactRepository("localize-bot/pipeline", 502),
            push_branch_prefix="guardian/prevention-",
            publication_actor=TrustedActor("acme", 12, actor_type),
            allowed_code_path_globs=("localize/*.py",),
            allowed_test_path_globs=("tests/**/*.py",),
            focused_test_argv=(("/opt/bin/pytest", "tests/unit/test_rules.py"),),
            sandbox_argv_prefix=("/usr/bin/guardian-sandbox-wrapper",),
            max_changed_files=4,
            max_changed_bytes=262_144,
        )


def test_parses_explicit_bounded_closed_pr_backfill_policy(tmp_path: Path) -> None:
    config_text = _minimal_config().replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 365\n"
        "      max_prs_per_poll: 4\n"
        "    trusted_reviewers:\n",
        1,
    )

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.repositories[0].closed_pr_backfill == ClosedPrBackfillPolicy(
        lookback_days=365,
        max_prs_per_poll=4,
    )


def test_parses_explicit_historical_remediation_authority(tmp_path: Path) -> None:
    config_text = _allow_remediation_namespace(_config_with_prevention()).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 365\n"
        "      max_prs_per_poll: 4\n"
        "      remediation:\n"
        "        push_repository:\n"
        "          full_name: localize-bot/widgets\n"
        "          id: 84\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        "        publication_actor: {login: localize-bot, id: 11, type: User}\n"
        "    trusted_reviewers:\n",
        1,
    ).replace(
        "repositories:\n",
        "mode: propose-prevention\n"
        "limits:\n"
        "  max_model_calls_per_day: 4\n"
        "repositories:\n",
        1,
    )

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.repositories[0].closed_pr_backfill == ClosedPrBackfillPolicy(
        lookback_days=365,
        max_prs_per_poll=4,
        remediation=HistoricalRemediationPolicy(
            push_repository=ExactRepository("localize-bot/widgets", 84),
            push_branch_prefix="localization/guardian-remediation-",
            publication_actor=TrustedActor("localize-bot", 11, "User"),
        ),
    )


@pytest.mark.parametrize("mode", ["observe", "prepare"])
def test_historical_remediation_may_remain_dormant_in_read_only_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    config_text = _allow_remediation_namespace(_minimal_config()).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository: {full_name: localize-bot/widgets, id: 84}\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        "        publication_actor: {login: localize-bot, id: 11, type: User}\n"
        "    trusted_reviewers:\n",
        1,
    ).replace("repositories:\n", f"mode: {mode}\nrepositories:\n", 1)

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.mode.value == mode
    assert config.repositories[0].closed_pr_backfill.remediation is not None
    assert config.limits.max_remediation_drafts_per_run == 0


def test_historical_remediation_allows_apply_mode_without_prevention(
    tmp_path: Path,
) -> None:
    config_text = _allow_remediation_namespace(_minimal_config()).replace(
        "repositories:\n",
        "mode: apply-owned-translations\nrepositories:\n",
        1,
    ).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository: {full_name: localize-bot/widgets, id: 84}\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        "        publication_actor: {login: localize-bot, id: 11, type: User}\n"
        "    trusted_reviewers:\n",
        1,
    )

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.mode is GuardianMode.APPLY_OWNED_TRANSLATIONS
    assert config.repositories[0].prevention is None
    assert config.repositories[0].closed_pr_backfill.remediation is not None


@pytest.mark.parametrize(
    ("old", "new", "offending"),
    [
        (
            "full_name: localize-bot/widgets\n          id: 84",
            "full_name: another-bot/widgets\n          id: 85",
            "allowed_head_repositories",
        ),
        (
            "push_branch_prefix: localization/guardian-remediation-",
            "push_branch_prefix: guardian/remediation-",
            "allowed_branch_globs",
        ),
    ],
)
def test_historical_remediation_must_create_a_guardian_eligible_pull(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    config_text = _allow_remediation_namespace(_config_with_prevention()).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository:\n"
        "          full_name: localize-bot/widgets\n"
        "          id: 84\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        "        publication_actor: {login: localize-bot, id: 11, type: User}\n"
        "    trusted_reviewers:\n",
        1,
    ).replace(
        "repositories:\n",
        "mode: propose-prevention\n"
        "limits:\n"
        "  max_model_calls_per_day: 4\n"
        "repositories:\n",
        1,
    )
    config_text = config_text.replace(old, new, 1)

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    ("actor", "match"),
    [
        ("{login: acme, id: 12, type: Organization}", "publication_actor"),
        ("{login: renamed-bot, id: 999, type: Bot}", "publication_actor"),
    ],
)
def test_historical_remediation_requires_one_exact_user_publication_actor(
    tmp_path: Path,
    actor: str,
    match: str,
) -> None:
    config_text = _allow_remediation_namespace(_minimal_config()).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository: {full_name: localize-bot/widgets, id: 84}\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        f"        publication_actor: {actor}\n"
        "    trusted_reviewers:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match=match):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize("login", ['"localize\\nbot"', '"localize\\0bot"'])
def test_historical_remediation_rejects_non_single_line_publication_actor_login(
    tmp_path: Path,
    login: str,
) -> None:
    config_text = _allow_remediation_namespace(_minimal_config()).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository: {full_name: localize-bot/widgets, id: 84}\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        f"        publication_actor: {{login: {login}, id: 11, type: User}}\n"
        "    trusted_reviewers:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match=r"publication_actor\.login"):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    ("old", "new", "offending"),
    [
        (
            "push_branch_prefix: localization/guardian-remediation-",
            "push_branch_prefix: refs/heads/remediation-",
            "branch prefix",
        ),
        (
            "push_branch_prefix: localization/guardian-remediation-",
            f"push_branch_prefix: {'a' * 192}",
            "leave room",
        ),
        (
            "full_name: localize-bot/widgets\n          id: 84",
            "full_name: acme/widgets\n          id: 84",
            "repository identity",
        ),
        (
            "full_name: localize-bot/widgets\n          id: 84",
            "full_name: localize-bot/widgets\n          id: 42",
            "repository identity",
        ),
    ],
)
def test_rejects_unsafe_or_ambiguous_historical_remediation_authority(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    config_text = _allow_remediation_namespace(_config_with_prevention()).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository:\n"
        "          full_name: localize-bot/widgets\n"
        "          id: 84\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        "        publication_actor: {login: localize-bot, id: 11, type: User}\n"
        "    trusted_reviewers:\n",
        1,
    ).replace("repositories:\n", "mode: propose-prevention\nrepositories:\n", 1)
    config_text = config_text.replace(old, new, 1)

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize("actor_type", ["Bot", "Organization"])
def test_historical_remediation_policy_rejects_invalid_direct_construction(
    actor_type: str,
) -> None:
    with pytest.raises(ValueError, match="push_branch_prefix"):
        HistoricalRemediationPolicy(
            push_repository=ExactRepository("localize-bot/widgets", 84),
            push_branch_prefix="refs/heads/remediation-",
            publication_actor=TrustedActor("localize-bot", 11, "User"),
        )

    with pytest.raises(ValueError, match="User identity"):
        HistoricalRemediationPolicy(
            push_repository=ExactRepository("localize-bot/widgets", 84),
            push_branch_prefix="localization/guardian-remediation-",
            publication_actor=TrustedActor("acme", 12, actor_type),
        )

    remediation = HistoricalRemediationPolicy(
        push_repository=ExactRepository("acme/widgets", 84),
        push_branch_prefix="localization/guardian-remediation-",
        publication_actor=TrustedActor("localize-bot", 11, "User"),
    )
    with pytest.raises(ValueError, match="ambiguous repository identity"):
        RepositoryPolicy(
            base_repo="acme/widgets",
            base_repo_id=42,
            base_branch="main",
            allowed_pr_authors=(TrustedActor("localize-bot", 11, "User"),),
            allowed_head_owners=(TrustedActor("acme", 12, "Organization"),),
            allowed_head_repositories=(
                AllowedHeadRepository("localize-bot/widgets", 84),
            ),
            allowed_branch_globs=("localization/**",),
            allowed_path_globs=("l10n/**",),
            pipeline_config_path="config.yaml",
            source_locale="en",
            trusted_reviewers={"ru": (TrustedActor("reviewer", 101, "User"),)},
            trusted_bots={},
            closed_pr_backfill=ClosedPrBackfillPolicy(
                lookback_days=30,
                max_prs_per_poll=2,
                remediation=remediation,
            ),
        )


def test_repository_policy_requires_remediation_head_scope_not_author_membership(
) -> None:
    common = {
        "base_repo": "acme/widgets",
        "base_repo_id": 42,
        "base_branch": "main",
        "allowed_pr_authors": (TrustedActor("translation-bot", 77, "Bot"),),
        "allowed_head_owners": (TrustedActor("acme", 12, "Organization"),),
        "allowed_head_repositories": (
            AllowedHeadRepository("localize-bot/widgets", 84),
        ),
        "allowed_branch_globs": ("localization/**",),
        "allowed_path_globs": ("l10n/**",),
        "pipeline_config_path": "config.yaml",
        "source_locale": "en",
        "trusted_reviewers": {
            "ru": (TrustedActor("reviewer", 101, "User"),),
        },
        "trusted_bots": {},
    }

    with pytest.raises(ValueError, match="allowed head repository"):
        RepositoryPolicy(
            **{
                **common,
                "allowed_branch_globs": (
                    "localization/guardian-remediation-*",
                ),
            },
            closed_pr_backfill=ClosedPrBackfillPolicy(
                lookback_days=30,
                max_prs_per_poll=2,
                remediation=HistoricalRemediationPolicy(
                    push_repository=ExactRepository("another-bot/widgets", 85),
                    push_branch_prefix="localization/guardian-remediation-",
                    publication_actor=TrustedActor("localize-bot", 11, "User"),
                ),
            ),
        )
    with pytest.raises(ValueError, match="allowed head branch"):
        RepositoryPolicy(
            **common,
            closed_pr_backfill=ClosedPrBackfillPolicy(
                lookback_days=30,
                max_prs_per_poll=2,
                remediation=HistoricalRemediationPolicy(
                    push_repository=ExactRepository("localize-bot/widgets", 84),
                    push_branch_prefix="guardian/remediation-",
                    publication_actor=TrustedActor("localize-bot", 11, "User"),
                ),
            ),
        )

    policy = RepositoryPolicy(
        **{
            **common,
            "allowed_branch_globs": (
                "localization/guardian-remediation-*",
            ),
        },
        closed_pr_backfill=ClosedPrBackfillPolicy(
            lookback_days=30,
            max_prs_per_poll=2,
            remediation=HistoricalRemediationPolicy(
                push_repository=ExactRepository("localize-bot/widgets", 84),
                push_branch_prefix="localization/guardian-remediation-",
                publication_actor=TrustedActor("machine-user", 99, "User"),
            ),
        ),
    )

    assert policy.allowed_pr_authors == (TrustedActor("translation-bot", 77, "Bot"),)
    assert policy.closed_pr_backfill.remediation.publication_actor == TrustedActor(
        "machine-user", 99, "User"
    )


def test_enabled_publication_policies_require_one_github_actor_identity() -> None:
    first_actor = TrustedActor("localize-bot", 11, "User")
    second_actor = TrustedActor("other-bot", 99, "User")
    remediation = HistoricalRemediationPolicy(
        push_repository=ExactRepository("localize-bot/widgets", 84),
        push_branch_prefix="localization/guardian-remediation-",
        publication_actor=first_actor,
    )
    policy = RepositoryPolicy(
        base_repo="acme/widgets",
        base_repo_id=42,
        base_branch="main",
        allowed_pr_authors=(first_actor,),
        allowed_head_owners=(TrustedActor("acme", 12, "Organization"),),
        allowed_head_repositories=(
            AllowedHeadRepository("localize-bot/widgets", 84),
        ),
        allowed_branch_globs=("localization/guardian-remediation-*",),
        allowed_path_globs=("l10n/**",),
        pipeline_config_path="config.yaml",
        source_locale="en",
        trusted_reviewers={
            "ru": (TrustedActor("reviewer", 101, "User"),),
        },
        trusted_bots={},
        closed_pr_backfill=ClosedPrBackfillPolicy(
            lookback_days=30,
            max_prs_per_poll=2,
            remediation=remediation,
        ),
        publication_actor=first_actor,
    )
    conflicting = replace(
        policy,
        base_repo="example/widgets",
        base_repo_id=43,
        allowed_pr_authors=(second_actor,),
        closed_pr_backfill=replace(
            policy.closed_pr_backfill,
            remediation=replace(remediation, publication_actor=second_actor),
        ),
        publication_actor=second_actor,
    )

    with pytest.raises(ValueError, match="one GitHub actor identity"):
        GuardianConfig(
            repositories=(policy, conflicting),
            mode=GuardianMode.APPLY_OWNED_TRANSLATIONS,
            limits=replace(
                GuardianConfig(repositories=(policy,)).limits,
                max_remediation_drafts_per_run=1,
            ),
        )


def test_config_rejects_different_prevention_and_remediation_actors(
    tmp_path: Path,
) -> None:
    config_text = _allow_remediation_namespace(_config_with_prevention()).replace(
        "    allowed_pr_authors:\n"
        "      - {login: localize-bot, id: 11, type: User}\n",
        "    allowed_pr_authors:\n"
        "      - {login: localize-bot, id: 11, type: User}\n"
        "      - {login: other-bot, id: 99, type: User}\n",
        1,
    ).replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 2\n"
        "      remediation:\n"
        "        push_repository: {full_name: localize-bot/widgets, id: 84}\n"
        "        push_branch_prefix: localization/guardian-remediation-\n"
        "        publication_actor: {login: other-bot, id: 99, type: User}\n"
        "    trusted_reviewers:\n",
        1,
    )
    config_text = (
        "mode: propose-prevention\nlimits:\n"
        "  max_model_calls_per_day: 4\n"
        "  max_remediation_drafts_per_run: 1\n"
        + config_text
    )

    with pytest.raises(GuardianConfigError, match="one GitHub actor identity"):
        load_guardian_config(_write_config(tmp_path, config_text))

@pytest.mark.parametrize("invalid_id", [True, 42.0, "42"])
@pytest.mark.parametrize(
    "factory",
    [
        lambda value: TrustedActor("actor", value, "User"),
        lambda value: AllowedHeadRepository("acme/widgets", value),
        lambda value: ExactRepository("acme/widgets", value),
    ],
)
def test_typed_github_identities_require_native_integer_ids(
    invalid_id: object,
    factory,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        factory(invalid_id)


def test_typed_github_actor_rejects_non_string_role_as_value_error() -> None:
    with pytest.raises(ValueError, match="type"):
        TrustedActor("actor", 42, ["Bot"])  # type: ignore[arg-type]


def test_direct_repository_policy_rejects_role_and_identity_bypasses() -> None:
    policy = parse_guardian_config(yaml.safe_load(_minimal_config())).repositories[0]

    with pytest.raises(ValueError, match=r"trusted_reviewers\.ru.*User"):
        replace(
            policy,
            trusted_reviewers={
                "ru": (TrustedActor("review-bot", 303, "Bot"),),
            },
        )
    with pytest.raises(ValueError, match="unique across reviewer and bot"):
        replace(
            policy,
            trusted_bots={
                "ru": (TrustedActor("same-id-bot", 101, "Bot"),),
            },
        )
    with pytest.raises(ValueError, match="allowed_pr_authors.*allowed roles"):
        replace(
            policy,
            allowed_pr_authors=(TrustedActor("acme", 12, "Organization"),),
        )
    with pytest.raises(ValueError, match="duplicate actor IDs"):
        replace(
            policy,
            allowed_pr_authors=(
                TrustedActor("translation-bot", 11, "Bot"),
                TrustedActor("renamed-bot", 11, "Bot"),
            ),
        )
    with pytest.raises(ValueError, match="duplicate identities"):
        replace(
            policy,
            allowed_head_repositories=(
                AllowedHeadRepository("localize-bot/widgets", 84),
                AllowedHeadRepository("LOCALIZE-BOT/widgets", 85),
            ),
        )
    with pytest.raises(ValueError, match="allowed_path_globs.*duplicates"):
        replace(policy, allowed_path_globs=("l10n/**", "l10n/**"))


def test_direct_repository_and_config_freeze_mutable_authority_inputs() -> None:
    parsed = parse_guardian_config(yaml.safe_load(_minimal_config())).repositories[0]
    authors = list(parsed.allowed_pr_authors)
    owners = list(parsed.allowed_head_owners)
    head_repositories = list(parsed.allowed_head_repositories)
    branch_globs = list(parsed.allowed_branch_globs)
    path_globs = list(parsed.allowed_path_globs)
    reviewer_accounts = list(parsed.trusted_reviewers["ru"])
    bot_accounts = list(parsed.trusted_bots["ru"])
    reviewers = {"ru": reviewer_accounts}
    bots = {"ru": bot_accounts}

    policy = RepositoryPolicy(
        base_repo=parsed.base_repo,
        base_repo_id=parsed.base_repo_id,
        base_branch=parsed.base_branch,
        allowed_pr_authors=authors,  # type: ignore[arg-type]
        allowed_head_owners=owners,  # type: ignore[arg-type]
        allowed_head_repositories=head_repositories,  # type: ignore[arg-type]
        allowed_branch_globs=branch_globs,  # type: ignore[arg-type]
        allowed_path_globs=path_globs,  # type: ignore[arg-type]
        pipeline_config_path=parsed.pipeline_config_path,
        source_locale=parsed.source_locale,
        trusted_reviewers=reviewers,  # type: ignore[arg-type]
        trusted_bots=bots,  # type: ignore[arg-type]
    )
    repositories = [policy]
    config = GuardianConfig(repositories=repositories, mode="observe")  # type: ignore[arg-type]

    authors.clear()
    owners.clear()
    head_repositories.clear()
    branch_globs.clear()
    path_globs.clear()
    reviewer_accounts.clear()
    bot_accounts.clear()
    reviewers.clear()
    bots.clear()
    repositories.clear()

    assert config.mode is GuardianMode.OBSERVE
    assert config.repositories == (policy,)
    assert policy.allowed_pr_authors == parsed.allowed_pr_authors
    assert policy.allowed_head_owners == parsed.allowed_head_owners
    assert policy.allowed_head_repositories == parsed.allowed_head_repositories
    assert policy.allowed_branch_globs == parsed.allowed_branch_globs
    assert policy.allowed_path_globs == parsed.allowed_path_globs
    assert policy.trusted_reviewers_for("ru") == parsed.trusted_reviewers_for("ru")
    assert policy.trusted_bots_for("ru") == parsed.trusted_bots_for("ru")
    with pytest.raises(TypeError):
        policy.trusted_reviewers["de"] = ()  # type: ignore[index]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repositories": ()}, "repositories.*non-empty"),
        ({"repositories": {"policy": object()}}, "repositories.*sequence"),
        ({"mode": "unsafe"}, "mode"),
        ({"repositories": (object(),)}, "RepositoryPolicy"),
        ({"limits": object()}, "GuardianLimits"),
        ({"runtime": object()}, "GuardianRuntime"),
        ({"schedule": object()}, "GuardianSchedule"),
    ],
)
def test_direct_guardian_config_rejects_untyped_or_empty_policy(
    kwargs: dict[str, object],
    message: str,
) -> None:
    policy = parse_guardian_config(yaml.safe_load(_minimal_config())).repositories[0]
    values: dict[str, object] = {"repositories": (policy,)}
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        GuardianConfig(**values)  # type: ignore[arg-type]


def test_pipeline_bundle_digest_rejects_mixed_key_types_as_value_error() -> None:
    with pytest.raises(ValueError, match="path/byte pairs"):
        pipeline_config_bundle_digest(  # type: ignore[arg-type]
            {"config.yaml": b"safe", 1: b"unsafe"}
        )


@pytest.mark.parametrize("invalid_id", [True, 42.0, "42"])
def test_repository_policy_requires_a_native_integer_base_repository_id(
    invalid_id: object,
) -> None:
    with pytest.raises(ValueError, match="Base repository id"):
        RepositoryPolicy(
            base_repo="acme/widgets",
            base_repo_id=invalid_id,  # type: ignore[arg-type]
            base_branch="main",
            allowed_pr_authors=(TrustedActor("localize-bot", 11, "Bot"),),
            allowed_head_owners=(TrustedActor("acme", 12, "Organization"),),
            allowed_head_repositories=(
                AllowedHeadRepository("localize-bot/widgets", 84),
            ),
            allowed_branch_globs=("localization/**",),
            allowed_path_globs=("l10n/**",),
            pipeline_config_path="config.yaml",
            source_locale="en",
            trusted_reviewers={},
            trusted_bots={},
        )


def test_repository_policy_requires_glob_to_cover_every_generated_branch() -> None:
    common = {
        "base_repo": "acme/widgets",
        "base_repo_id": 42,
        "base_branch": "main",
        "allowed_pr_authors": (TrustedActor("localize-bot", 11, "Bot"),),
        "allowed_head_owners": (TrustedActor("acme", 12, "Organization"),),
        "allowed_head_repositories": (
            AllowedHeadRepository("localize-bot/widgets", 84),
        ),
        "allowed_path_globs": ("l10n/**",),
        "pipeline_config_path": "config.yaml",
        "source_locale": "en",
        "trusted_reviewers": {
            "ru": (TrustedActor("reviewer", 101, "User"),),
        },
        "trusted_bots": {},
        "closed_pr_backfill": ClosedPrBackfillPolicy(
            lookback_days=30,
            max_prs_per_poll=2,
            remediation=HistoricalRemediationPolicy(
                push_repository=ExactRepository("localize-bot/widgets", 84),
                push_branch_prefix="localization/remediation-",
                publication_actor=TrustedActor("localize-bot", 11, "User"),
            ),
        ),
    }

    with pytest.raises(ValueError, match="allowed head branch"):
        RepositoryPolicy(
            **common,
            allowed_branch_globs=("localization/remediation-0*",),
        )

    policy = RepositoryPolicy(
        **common,
        allowed_branch_globs=("localization/remediation-*",),
    )
    assert policy.allowed_branch_globs == ("localization/remediation-*",)


@pytest.mark.parametrize("value", [0, 3])
def test_parses_bounded_remediation_draft_limit(
    tmp_path: Path,
    value: int,
) -> None:
    config_text = _minimal_config().replace(
        "repositories:\n",
        f"limits:\n  max_remediation_drafts_per_run: {value}\nrepositories:\n",
        1,
    )

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.limits.max_remediation_drafts_per_run == value


@pytest.mark.parametrize("value", ["-1", "true", "1.5"])
def test_rejects_invalid_remediation_draft_limit(
    tmp_path: Path,
    value: str,
) -> None:
    config_text = _minimal_config().replace(
        "repositories:\n",
        f"limits:\n  max_remediation_drafts_per_run: {value}\nrepositories:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match="max_remediation_drafts_per_run"):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lookback_days", "0"),
        ("lookback_days", "-1"),
        ("lookback_days", "3651"),
        ("lookback_days", "true"),
        ("lookback_days", "1.5"),
        ("max_prs_per_poll", "0"),
        ("max_prs_per_poll", "-1"),
        ("max_prs_per_poll", "101"),
        ("max_prs_per_poll", "true"),
        ("max_prs_per_poll", "1.5"),
    ],
)
def test_rejects_unbounded_or_non_integer_closed_pr_backfill_values(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    config_text = _minimal_config().replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        f"      lookback_days: {value if field == 'lookback_days' else '30'}\n"
        f"      max_prs_per_poll: {value if field == 'max_prs_per_poll' else '4'}\n"
        "    trusted_reviewers:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match=field):
        load_guardian_config(_write_config(tmp_path, config_text))


def test_closed_pr_backfill_policy_rejects_invalid_direct_construction() -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        ClosedPrBackfillPolicy(lookback_days=True, max_prs_per_poll=1)
    with pytest.raises(ValueError, match="lookback_days"):
        ClosedPrBackfillPolicy(lookback_days=3651, max_prs_per_poll=1)
    with pytest.raises(ValueError, match="max_prs_per_poll"):
        ClosedPrBackfillPolicy(lookback_days=30, max_prs_per_poll=101)


def test_closed_pr_backfill_rejects_unknown_keys(tmp_path: Path) -> None:
    config_text = _minimal_config().replace(
        "    trusted_reviewers:\n",
        "    closed_pr_backfill:\n"
        "      lookback_days: 30\n"
        "      max_prs_per_poll: 4\n"
        "      include_drafts: true\n"
        "    trusted_reviewers:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match="include_drafts"):
        load_guardian_config(_write_config(tmp_path, config_text))


def test_parses_operator_pipeline_config_and_daily_schedule(tmp_path: Path) -> None:
    config_text = _minimal_config().replace(
        "repositories:\n",
        "schedule:\n  hour: 5\n  minute: 15\nrepositories:\n",
        1,
    ).replace(
        "    pipeline_config_path: .localize/config.yaml\n",
        "    pipeline_config_source: operator\n"
        "    pipeline_config_path: projects/widgets/config.yaml\n",
        1,
    )

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.schedule.hour == 5
    assert config.schedule.minute == 15
    assert config.repositories[0].pipeline_config_source is PipelineConfigSource.OPERATOR
    assert config.repositories[0].pipeline_config_path == "projects/widgets/config.yaml"


@pytest.mark.parametrize(
    "schedule_yaml",
    [
        "schedule: {hour: -1, minute: 0}",
        "schedule: {hour: 24, minute: 0}",
        "schedule: {hour: 0, minute: -1}",
        "schedule: {hour: 0, minute: 60}",
        "schedule: {hour: 1.5, minute: 0}",
        "schedule: {hour: 1.0, minute: 0}",
        "schedule: {hour: 0, minute: 0, timezone: UTC}",
    ],
)
def test_rejects_invalid_daily_schedule(
    tmp_path: Path,
    schedule_yaml: str,
) -> None:
    config_text = _minimal_config().replace(
        "repositories:\n",
        f"{schedule_yaml}\nrepositories:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match="schedule"):
        load_guardian_config(_write_config(tmp_path, config_text))


def test_rejects_unknown_pipeline_config_source(tmp_path: Path) -> None:
    config_text = _minimal_config().replace(
        "    pipeline_config_path: .localize/config.yaml\n",
        "    pipeline_config_source: pull-request\n"
        "    pipeline_config_path: .localize/config.yaml\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match="pipeline_config_source"):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize("repository", ["../widgets", "acme/..", "./widgets", "acme/."])
def test_rejects_repository_path_components(
    tmp_path: Path, repository: str
) -> None:
    config_text = _minimal_config().replace("acme/widgets", repository, 1)

    with pytest.raises(GuardianConfigError, match="base_repo"):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize("repository", ["../pipeline", "acme/..", "./pipeline", "acme/."])
def test_typed_exact_repository_rejects_path_components(repository: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        ExactRepository(full_name=repository, id=501)


def test_requires_exact_base_branch_authority(tmp_path: Path) -> None:
    config_text = _minimal_config().replace("    base_branch: main\n", "", 1)

    with pytest.raises(GuardianConfigError, match="base_branch.*required"):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    "original, replacement",
    [
        ("base_repo: acme/widgets", 'base_repo: "acme/widgets\\n"'),
        ("login: localize-bot", 'login: "localize-bot\\nforged"'),
        (
            "full_name: localize-bot/widgets",
            'full_name: "localize-bot/widgets\\n"',
        ),
        (
            'allowed_branch_globs: ["localization/**"]',
            'allowed_branch_globs: ["localization/**\\n"]',
        ),
        (
            'allowed_path_globs: ["src/main/resources/l10n/**"]',
            'allowed_path_globs: ["src/main/resources/l10n/**\\n"]',
        ),
        (
            "pipeline_config_path: .localize/config.yaml",
            'pipeline_config_path: ".localize/config.yaml\\n"',
        ),
        ("source_locale: en", 'source_locale: "en\\n"'),
        ("      ru:", '      "ru\\n":'),
    ],
)
def test_rejects_control_characters_in_every_authority_string(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    config_text = _minimal_config().replace(original, replacement, 1)

    with pytest.raises(GuardianConfigError):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    "field, unsafe_value",
    [
        ("allowed_branch_globs", "refs/heads/main"),
        ("allowed_branch_globs", "localization/[ab]"),
        ("allowed_path_globs", ".git/**"),
        ("pipeline_config_path", ".localize/*.yaml"),
    ],
)
def test_rejects_unsafe_authority_paths_and_branch_globs(
    tmp_path: Path,
    field: str,
    unsafe_value: str,
) -> None:
    replacements = {
        "allowed_branch_globs": (
            'allowed_branch_globs: ["localization/**"]',
            f'allowed_branch_globs: ["{unsafe_value}"]',
        ),
        "allowed_path_globs": (
            'allowed_path_globs: ["src/main/resources/l10n/**"]',
            f'allowed_path_globs: ["{unsafe_value}"]',
        ),
        "pipeline_config_path": (
            "pipeline_config_path: .localize/config.yaml",
            f'pipeline_config_path: "{unsafe_value}"',
        ),
    }
    original, replacement = replacements[field]

    with pytest.raises(GuardianConfigError):
        load_guardian_config(
            _write_config(
                tmp_path,
                _minimal_config().replace(original, replacement, 1),
            )
        )


@pytest.mark.parametrize(
    "raw_mode, expected",
    [
        ("observe", GuardianMode.OBSERVE),
        ("prepare", GuardianMode.PREPARE),
        ("apply-owned-translations", GuardianMode.APPLY_OWNED_TRANSLATIONS),
    ],
)
def test_loads_each_supported_mode(
    tmp_path: Path,
    raw_mode: str,
    expected: GuardianMode,
) -> None:
    config_text = f"mode: {raw_mode}\n" + _minimal_config()

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.mode is expected
    assert config.report_only is (expected is GuardianMode.OBSERVE)


def test_loads_explicit_prevention_policy_for_propose_mode(tmp_path: Path) -> None:
    config_text = """mode: propose-prevention
limits:
  max_model_calls_per_day: 4
""" + _config_with_prevention()

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.mode is GuardianMode.PROPOSE_PREVENTION
    assert config.report_only is False
    assert config.repositories[0].prevention == PreventionPolicy(
        target_repository=ExactRepository(
            full_name="acme/localization-pipeline",
            id=501,
        ),
        target_base_branch="main",
        push_repository=ExactRepository(
            full_name="localize-bot/localization-pipeline",
            id=502,
        ),
        push_branch_prefix="guardian/prevention-",
        publication_actor=TrustedActor("localize-bot", 11, "User"),
        allowed_code_path_globs=("localize/**/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(
            (
                "/opt/localize-guardian/bin/pytest",
                "tests/unit/test_rules.py",
                "-q",
            ),
            ("/opt/localize-guardian/bin/ruff", "check", "localize", "tests"),
        ),
        sandbox_argv_prefix=("/opt/localize-guardian/bin/sandbox-wrapper",),
        max_changed_files=4,
        max_changed_bytes=262144,
    )


def test_prevention_requires_one_typed_numeric_publication_actor(
    tmp_path: Path,
) -> None:
    actor_block = """      publication_actor:
        login: localize-bot
        id: 11
        type: User
"""
    missing = _config_with_prevention().replace(actor_block, "", 1)
    with pytest.raises(GuardianConfigError, match="publication_actor"):
        load_guardian_config(_write_config(tmp_path, missing))

    for original, replacement in (
        ("        id: 11\n", "        id: true\n"),
        ("        type: User\n", "        type: Bot\n"),
    ):
        malformed = _config_with_prevention().replace(original, replacement, 1)
        with pytest.raises(GuardianConfigError, match="publication_actor"):
            load_guardian_config(_write_config(tmp_path, malformed))


def test_prevention_rejects_multiline_publication_actor_login(tmp_path: Path) -> None:
    malformed = _config_with_prevention().replace(
        "        login: localize-bot\n",
        '        login: "localize-bot\\nforged"\n',
        1,
    )

    with pytest.raises(GuardianConfigError, match=r"publication_actor\.login"):
        load_guardian_config(_write_config(tmp_path, malformed))


@pytest.mark.parametrize(
    "max_drafts,max_attempts,daily_limit",
    [
        (1, 2, 19.99),
        (2, 1, 14.99),
    ],
)
def test_propose_mode_requires_budget_for_assessment_and_every_authoring_call(
    tmp_path: Path,
    max_drafts: int,
    max_attempts: int,
    daily_limit: float,
) -> None:
    config_text = f"""mode: propose-prevention
runtime:
  codex_auth_mode: api-key
  codex_api_key_command: [/opt/bin/model-token]
limits:
  max_prevention_drafts_per_run: {max_drafts}
  max_attempts: {max_attempts}
  max_model_calls_per_day: {max_attempts * (1 + max_drafts)}
  daily_cost_limit_usd: {daily_limit}
  model_call_reservation_usd: 5
""" + _config_with_prevention()

    with pytest.raises(
        GuardianConfigError,
        match="assessment plus.*prevention authoring",
    ):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    "max_drafts,max_attempts,daily_limit",
    [
        (0, 2, 5.0),
        (1, 2, 20.0),
        (2, 1, 15.0),
    ],
)
def test_propose_mode_accepts_exact_required_reservation_capacity(
    tmp_path: Path,
    max_drafts: int,
    max_attempts: int,
    daily_limit: float,
) -> None:
    config_text = f"""mode: propose-prevention
runtime:
  codex_auth_mode: api-key
  codex_api_key_command: [/opt/bin/model-token]
limits:
  max_prevention_drafts_per_run: {max_drafts}
  max_attempts: {max_attempts}
  max_model_calls_per_day: {max_attempts * (1 + max_drafts)}
  daily_cost_limit_usd: {daily_limit}
  model_call_reservation_usd: 5
""" + _config_with_prevention()

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.limits.max_prevention_drafts_per_run == max_drafts
    assert config.limits.daily_cost_limit_usd == daily_limit


def test_prevention_private_target_opt_in_is_separate_and_explicit(
    tmp_path: Path,
) -> None:
    missing = _config_with_prevention().replace(
        "      private_target_model_opt_in: false\n",
        "",
        1,
    )
    with pytest.raises(GuardianConfigError, match="private_target_model_opt_in"):
        load_guardian_config(_write_config(tmp_path, missing))

    enabled = _config_with_prevention().replace(
        "      private_target_model_opt_in: false",
        "      private_target_model_opt_in: true",
        1,
    )
    config = load_guardian_config(_write_config(tmp_path, enabled))
    assert config.repositories[0].prevention is not None
    assert config.repositories[0].prevention.private_target_model_opt_in is True


def test_propose_mode_requires_prevention_policy_for_every_repository(
    tmp_path: Path,
) -> None:
    first_missing = "mode: propose-prevention\n" + _minimal_config()
    with pytest.raises(GuardianConfigError, match="prevention.*required"):
        load_guardian_config(_write_config(tmp_path, first_missing))

    second_missing = "mode: propose-prevention\n" + _config_with_prevention() + """
  - base_repo: example/translations
    base_repo_id: 84
    base_branch: main
    allowed_pr_authors:
      - {login: translation-service, id: 31, type: User}
    allowed_head_owners:
      - {login: example, id: 32, type: Organization}
    allowed_head_repositories:
      - {full_name: translation-service/translations, id: 168}
    allowed_branch_globs: ["translate/**"]
    allowed_path_globs: ["apps/**/i18n/**"]
    pipeline_config_path: config.yaml
    source_locale: en
    trusted_reviewers:
      de:
        - {login: second-maintainer, id: 303, type: User}
    trusted_bots: {}
"""
    with pytest.raises(GuardianConfigError, match="repositories.1.prevention.*required"):
        load_guardian_config(_write_config(tmp_path, second_missing))


def test_lower_modes_may_carry_or_omit_prevention_policy(tmp_path: Path) -> None:
    prepared = load_guardian_config(
        _write_config(tmp_path, "mode: prepare\n" + _config_with_prevention())
    )
    observed = load_guardian_config(_write_config(tmp_path, _minimal_config()))

    assert prepared.repositories[0].prevention is not None
    assert observed.repositories[0].prevention is None


def test_loads_explicit_limits_and_private_repo_model_opt_in(tmp_path: Path) -> None:
    config_text = _minimal_config().replace(
        "repositories:\n",
        """limits:
  run_timeout_seconds: 120
  max_attempts: 1
  max_value_edits_per_run: 3
  max_prevention_drafts_per_run: 0
  max_model_calls_per_day: 2
  min_apply_confidence: 0.95
  raw_retention_days: 14
repositories:
""",
        1,
    ).replace(
        "  - base_repo: acme/widgets\n",
        "  - base_repo: acme/widgets\n    private_repo_model_opt_in: true\n",
        1,
    )

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.limits.run_timeout_seconds == 120
    assert config.limits.max_attempts == 1
    assert config.limits.max_value_edits_per_run == 3
    assert config.limits.max_prevention_drafts_per_run == 0
    assert config.limits.max_model_calls_per_day == 2
    assert config.limits.daily_cost_limit_usd is None
    assert config.limits.model_call_reservation_usd is None
    assert config.limits.min_apply_confidence == 0.95
    assert config.limits.raw_retention_days == 14
    assert config.repositories[0].private_repo_model_opt_in is True


def test_value_edit_limit_has_a_finite_draft_safety_bound(tmp_path: Path) -> None:
    config_text = _minimal_config().replace(
        "repositories:\n",
        "limits:\n  max_value_edits_per_run: 101\nrepositories:\n",
        1,
    )

    with pytest.raises(GuardianConfigError, match="max_value_edits_per_run"):
        load_guardian_config(_write_config(tmp_path, config_text))


def test_loads_secret_free_runtime_broker_and_codex_settings(tmp_path: Path) -> None:
    fingerprint = "A" * 40
    config_text = f"""runtime:
  codex_auth_mode: api-key
  codex_model: gpt-5.6-terra
  codex_reasoning_effort: high
  codex_executable: /opt/local/bin/codex
  git_executable: /opt/local/bin/git
  signing_program: /opt/local/bin/gpg
  github_token_command: [/opt/local/bin/gh, auth, token]
  codex_api_key_command: [/opt/local/bin/model-key-helper]
  signing_key: {fingerprint}
limits:
  daily_cost_limit_usd: 20
  model_call_reservation_usd: 5
""" + _minimal_config()

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.runtime.codex_model == "gpt-5.6-terra"
    assert config.runtime.codex_auth_mode is CodexAuthMode.API_KEY
    assert config.runtime.codex_reasoning_effort == "high"
    assert config.runtime.codex_executable == "/opt/local/bin/codex"
    assert config.runtime.git_executable == "/opt/local/bin/git"
    assert config.runtime.signing_program == "/opt/local/bin/gpg"
    assert config.runtime.github_token_command == (
        "/opt/local/bin/gh",
        "auth",
        "token",
    )
    assert config.runtime.codex_api_key_command == (
        "/opt/local/bin/model-key-helper",
    )
    assert config.runtime.signing_key == fingerprint
    assert config.runtime.signing_format is SigningFormat.OPENPGP
    assert config.runtime.signing_public_key is None


@pytest.mark.parametrize(
    ("kwargs", "offending"),
    [
        ({"run_timeout_seconds": 0}, "run_timeout_seconds"),
        ({"max_attempts": True}, "max_attempts"),
        ({"max_attempts": 3}, "max_attempts"),
        ({"max_value_edits_per_run": 101}, "max_value_edits_per_run"),
        ({"max_prevention_drafts_per_run": -1}, "max_prevention"),
        ({"max_remediation_drafts_per_run": -1}, "max_remediation"),
        ({"max_model_calls_per_day": 0}, "max_model_calls"),
        ({"raw_retention_days": 0}, "raw_retention_days"),
        ({"daily_cost_limit_usd": 0, "model_call_reservation_usd": 1}, "daily"),
        (
            {"daily_cost_limit_usd": float("nan"), "model_call_reservation_usd": 1},
            "daily",
        ),
        (
            {"daily_cost_limit_usd": 2, "model_call_reservation_usd": float("inf")},
            "reservation",
        ),
        ({"daily_cost_limit_usd": 2}, "set together"),
        (
            {"daily_cost_limit_usd": 1, "model_call_reservation_usd": 2},
            "must not exceed",
        ),
        ({"min_apply_confidence": -0.1}, "min_apply_confidence"),
        ({"min_apply_confidence": float("nan")}, "min_apply_confidence"),
    ],
)
def test_typed_guardian_limits_enforce_parser_bounds(
    kwargs: dict[str, object],
    offending: str,
) -> None:
    with pytest.raises(ValueError, match=offending):
        GuardianLimits(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "offending"),
    [
        ({"codex_model": ""}, "codex_model"),
        ({"codex_model": "x" * 4097}, "codex_model"),
        ({"codex_reasoning_effort": "none"}, "codex_reasoning_effort"),
        ({"codex_reasoning_effort": ["high"]}, "codex_reasoning_effort"),
        ({"codex_auth_mode": "other"}, "codex_auth_mode"),
        ({"codex_home": "relative/path"}, "codex_home"),
        ({"codex_home": "~/../shared"}, "codex_home"),
        ({"github_token_command": ()}, "github_token_command"),
        ({"github_token_command": {"helper"}}, "github_token_command"),
        (
            {"github_token_command": tuple("x" for _ in range(33))},
            "github_token_command",
        ),
        ({"github_token_command": ("sh", "-c", "read-token")}, "shell wrapper"),
        (
            {"github_token_command": ("helper", "TOKEN=committed-secret")},
            "credentials or environment assignments",
        ),
        (
            {"github_token_command": ("python3", "-c", "read_secret()")},
            "interpreter command string",
        ),
        (
            {"github_token_command": ("python3", "/opt/helpers/github.py")},
            "interpreter or command dispatcher",
        ),
        (
            {"github_token_command": ("node", "/opt/helpers/github.js")},
            "interpreter or command dispatcher",
        ),
        (
            {"github_token_command": ("nice", "/opt/helpers/github-token")},
            "interpreter or command dispatcher",
        ),
        (
            {"github_token_command": ("nohup", "/opt/helpers/github-token")},
            "interpreter or command dispatcher",
        ),
        (
            {"github_token_command": ("helper", "read")},
            "arguments",
        ),
        (
            {
                "codex_auth_mode": CodexAuthMode.API_KEY,
                "codex_api_key_command": ("helper", "read"),
            },
            "arguments",
        ),
        ({"codex_auth_mode": CodexAuthMode.API_KEY}, "codex_api_key_command"),
        ({"codex_api_key_command": ("helper",)}, "only valid"),
        ({"signing_key": "A" * 16}, "40- or 64-hex"),
        ({"signing_public_key": "/keys/guardian.pub"}, "only valid"),
        ({"signing_format": "x509"}, "signing_format"),
        ({"signing_format": SigningFormat.SSH}, "signing_key"),
        (
            {
                "signing_format": SigningFormat.SSH,
                "signing_key": "SHA256:" + "A" * 43,
            },
            "signing_public_key",
        ),
        (
            {
                "signing_format": SigningFormat.SSH,
                "signing_key": "SHA256:" + "A" * 43,
                "signing_public_key": "keys/guardian.pub",
                "signing_program": "/usr/bin/ssh-keygen",
            },
            "absolute POSIX",
        ),
        (
            {
                "signing_format": SigningFormat.SSH,
                "signing_key": "SHA256:" + "A" * 43,
                "signing_public_key": "/keys/guardian.pub",
            },
            "signing_program",
        ),
    ],
)
def test_typed_guardian_runtime_enforces_parser_policy(
    kwargs: dict[str, object],
    offending: str,
) -> None:
    with pytest.raises(ValueError, match=offending):
        GuardianRuntime(**kwargs)  # type: ignore[arg-type]


def test_typed_guardian_runtime_normalizes_parser_representations() -> None:
    runtime = GuardianRuntime(
        codex_auth_mode="api-key",  # type: ignore[arg-type]
        codex_api_key_command=["/opt/bin/model-token"],  # type: ignore[arg-type]
        github_token_command=["/opt/bin/github-token"],  # type: ignore[arg-type]
        signing_key="a" * 40,
    )
    limits = GuardianLimits(
        daily_cost_limit_usd=2,
        model_call_reservation_usd=1,
        min_apply_confidence=1,
    )

    assert runtime.codex_auth_mode is CodexAuthMode.API_KEY
    assert runtime.codex_api_key_command == ("/opt/bin/model-token",)
    assert runtime.github_token_command == ("/opt/bin/github-token",)
    assert runtime.signing_key == "A" * 40
    assert limits.daily_cost_limit_usd == 2.0
    assert limits.model_call_reservation_usd == 1.0
    assert limits.min_apply_confidence == 1.0


@pytest.mark.parametrize("typed_field", ["runtime", "limits"])
def test_parser_wraps_typed_model_rejections_as_guardian_config_errors(
    monkeypatch: pytest.MonkeyPatch,
    typed_field: str,
) -> None:
    if typed_field == "runtime":
        original = GuardianRuntime

        def rejecting_model(**kwargs):
            if kwargs:
                raise ValueError("runtime-only rejection")
            return original()

        monkeypatch.setattr("localize.guardian.config.GuardianRuntime", rejecting_model)
    else:
        original = GuardianLimits

        def rejecting_model(**kwargs):
            if kwargs:
                raise ValueError("limits-only rejection")
            return original()

        monkeypatch.setattr("localize.guardian.config.GuardianLimits", rejecting_model)

    with pytest.raises(
        GuardianConfigError,
        match=rf"at {typed_field}.*{typed_field}-only rejection",
    ):
        parse_guardian_config(yaml.safe_load(_minimal_config()))


def test_loads_exact_ssh_signing_identity(tmp_path: Path) -> None:
    fingerprint = "SHA256:" + "A" * 43
    config_text = f"""runtime:
  signing_format: ssh
  signing_program: /usr/bin/ssh-keygen
  signing_key: {fingerprint}
  signing_public_key: /keys/guardian.pub
""" + _minimal_config()

    config = load_guardian_config(_write_config(tmp_path, config_text))

    assert config.runtime.signing_format is SigningFormat.SSH
    assert config.runtime.signing_program == "/usr/bin/ssh-keygen"
    assert config.runtime.signing_key == fingerprint
    assert config.runtime.signing_public_key == "/keys/guardian.pub"


@pytest.mark.parametrize(
    "runtime_yaml, offending",
    [
        (
            "signing_format: ssh\n  signing_key: SHA256:" + "A" * 43,
            "signing_public_key",
        ),
        (
            "signing_format: ssh\n  signing_key: SHA256:" + "A" * 43
            + "\n  signing_public_key: /keys/guardian.pub",
            "signing_program",
        ),
        (
            "signing_format: ssh\n  signing_public_key: /keys/guardian.pub",
            "signing_key",
        ),
        (
            "signing_format: ssh\n  signing_key: " + "A" * 40
            + "\n  signing_public_key: /keys/guardian.pub",
            "SHA256",
        ),
        (
            "signing_format: ssh\n  signing_key: SHA256:" + "A" * 42
            + "\n  signing_public_key: /keys/guardian.pub",
            "SHA256",
        ),
        (
            "signing_format: ssh\n  signing_key: SHA256:" + "A" * 43
            + "\n  signing_public_key: keys/guardian.pub",
            "absolute",
        ),
        (
            "signing_format: openpgp\n  signing_key: " + "A" * 40
            + "\n  signing_public_key: /keys/guardian.pub",
            "only valid",
        ),
        ("signing_format: x509", "signing_format"),
    ],
)
def test_rejects_ambiguous_or_incomplete_signing_formats(
    tmp_path: Path,
    runtime_yaml: str,
    offending: str,
) -> None:
    yaml_text = f"runtime:\n  {runtime_yaml}\n" + _minimal_config()

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, yaml_text))


def test_chatgpt_auth_rejects_api_key_and_dollar_budget_configuration(
    tmp_path: Path,
) -> None:
    with pytest.raises(GuardianConfigError, match="codex_api_key_command.*api-key"):
        load_guardian_config(
            _write_config(
                tmp_path,
                "runtime:\n  codex_api_key_command: [/opt/bin/model-token]\n"
                + _minimal_config(),
            )
        )

    with pytest.raises(GuardianConfigError, match="daily_cost_limit_usd.*api-key"):
        load_guardian_config(
            _write_config(
                tmp_path,
                "limits:\n  daily_cost_limit_usd: 5\n" + _minimal_config(),
            )
        )


def test_api_key_auth_requires_helper_and_complete_spend_limits(tmp_path: Path) -> None:
    with pytest.raises(GuardianConfigError, match="codex_api_key_command"):
        load_guardian_config(
            _write_config(
                tmp_path,
                "runtime:\n  codex_auth_mode: api-key\n" + _minimal_config(),
            )
        )

    partial = """runtime:
  codex_auth_mode: api-key
  codex_api_key_command: [/opt/bin/model-token]
limits:
  daily_cost_limit_usd: 5
""" + _minimal_config()
    with pytest.raises(GuardianConfigError, match="model_call_reservation_usd"):
        load_guardian_config(_write_config(tmp_path, partial))


def test_daily_model_call_limit_covers_configured_retry_capacity(tmp_path: Path) -> None:
    config_text = """mode: propose-prevention
limits:
  max_attempts: 2
  max_prevention_drafts_per_run: 1
  max_model_calls_per_day: 3
""" + _config_with_prevention()

    with pytest.raises(GuardianConfigError, match="max_model_calls_per_day"):
        load_guardian_config(_write_config(tmp_path, config_text))


def test_retry_limit_matches_the_codex_driver_bound(tmp_path: Path) -> None:
    config_text = "limits:\n  max_attempts: 3\n" + _minimal_config()

    with pytest.raises(GuardianConfigError, match="max_attempts"):
        load_guardian_config(_write_config(tmp_path, config_text))


def test_daily_cost_limit_must_be_positive(tmp_path: Path) -> None:
    config_text = "limits:\n  daily_cost_limit_usd: 0\n" + _minimal_config()

    with pytest.raises(GuardianConfigError, match="daily_cost_limit_usd"):
        load_guardian_config(_write_config(tmp_path, config_text))


@pytest.mark.parametrize(
    "field",
    [
        "daily_cost_limit_usd",
        "model_call_reservation_usd",
        "min_apply_confidence",
    ],
)
@pytest.mark.parametrize("yaml_number", [".nan", ".inf", "-.inf"])
def test_rejects_non_finite_numeric_limits_as_guardian_config_error(
    tmp_path: Path,
    field: str,
    yaml_number: str,
) -> None:
    yaml_text = f"limits:\n  {field}: {yaml_number}\n" + _minimal_config()

    with pytest.raises(GuardianConfigError, match=rf"{field}.*finite"):
        load_guardian_config(_write_config(tmp_path, yaml_text))


@pytest.mark.parametrize(
    "yaml_text, offending_key",
    [
        (
            _minimal_config() + "trusted_reviewers: [global-maintainer]\n",
            "trusted_reviewers",
        ),
        (
            _minimal_config().replace(
                "    source_locale: en\n",
                "    source_locale: en\n    github_token: never-store-this-here\n",
            ),
            "github_token",
        ),
        (
            _minimal_config().replace(
                "    source_locale: en\n",
                "    source_locale: en\n    unexpected_policy: true\n",
            ),
            "unexpected_policy",
        ),
        (
            "limits:\n  unknown_limit: 1\n" + _minimal_config(),
            "unknown_limit",
        ),
        (
            "runtime:\n  codex_api_key: never-store-this-here\n" + _minimal_config(),
            "codex_api_key",
        ),
        (
            _config_with_prevention().replace(
                "      max_changed_bytes: 262144\n",
                "      max_changed_bytes: 262144\n"
                "      github_token: never-store-this-here\n",
                1,
            ),
            "github_token",
        ),
    ],
)
def test_rejects_unknown_and_secret_fields(
    tmp_path: Path,
    yaml_text: str,
    offending_key: str,
) -> None:
    with pytest.raises(GuardianConfigError, match=offending_key):
        load_guardian_config(_write_config(tmp_path, yaml_text))


@pytest.mark.parametrize(
    "runtime_yaml, offending",
    [
        ("codex_reasoning_effort: extreme", "codex_reasoning_effort"),
        ("codex_model: ''", "codex_model"),
        ("github_token_command: []", "github_token_command"),
        ('github_token_command: [gh, "bad\\nargument"]', "github_token_command"),
        ("codex_api_key_command: security", "codex_api_key_command"),
        ('signing_key: "bad\\nkey"', "signing_key"),
        ("signing_key: " + "A" * 16, "full 40- or 64-hex"),
        ("github_token_command: [sh, -c, echo-secret]", "shell wrapper"),
        (
            "codex_api_key_command: [python3, -c, print-secret]",
            "command string",
        ),
        (
            "github_token_command: [python3, /opt/helpers/github.py]",
            "interpreter or command dispatcher",
        ),
        (
            "github_token_command: [node, /opt/helpers/github.js]",
            "interpreter or command dispatcher",
        ),
        (
            "github_token_command: [nice, /opt/helpers/github-token]",
            "interpreter or command dispatcher",
        ),
        (
            "github_token_command: [nohup, /opt/helpers/github-token]",
            "interpreter or command dispatcher",
        ),
        (
            "github_token_command: [helper, read]",
            "arguments",
        ),
        (
            "github_token_command: [awk, -f, /opt/helpers/github.awk]",
            "arguments",
        ),
        (
            "github_token_command: [tclsh, /opt/helpers/github.tcl]",
            "arguments",
        ),
        (
            "github_token_command: [uv, run, /opt/helpers/github.py]",
            "arguments",
        ),
        (
            "github_token_command: [helper, TOKEN=committed-secret]",
            "credentials or environment assignments",
        ),
        (
            "codex_api_key_command: [helper, --api-key=committed-secret]",
            "credentials or environment assignments",
        ),
    ],
)
def test_rejects_unsafe_runtime_configuration(
    tmp_path: Path,
    runtime_yaml: str,
    offending: str,
) -> None:
    yaml_text = f"runtime:\n  {runtime_yaml}\n" + _minimal_config()

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, yaml_text))


@pytest.mark.parametrize(
    "old, new, offending",
    [
        (
            "full_name: acme/localization-pipeline",
            "full_name: acme",
            "target_repository.full_name",
        ),
        ("        id: 501", "        id: 0", "target_repository.id"),
        ("base_branch: main", "base_branch: ../main", "base_branch"),
        (
            "branch_prefix: guardian/prevention-",
            "branch_prefix: refs/heads/prevention-",
            "branch_prefix",
        ),
        (
            '"localize/**/*.py"',
            '"../localize/**/*.py"',
            "allowed_code_path_globs",
        ),
        (
            '"tests/**/*.py"',
            '".git/**/*.py"',
            "allowed_test_path_globs",
        ),
        (
            "max_changed_files: 4",
            "max_changed_files: 0",
            "max_changed_files",
        ),
        (
            "max_changed_bytes: 262144",
            "max_changed_bytes: 0",
            "max_changed_bytes",
        ),
    ],
)
def test_rejects_unsafe_prevention_identities_refs_paths_and_limits(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    yaml_text = _config_with_prevention().replace(old, new, 1)

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, yaml_text))


def test_accepts_exact_prevention_collection_and_utf8_byte_boundaries() -> None:
    raw = _raw_config_with_prevention()
    prevention = raw["repositories"][0]["prevention"]
    exact_string = "é" * 2048
    focused_commands = [
        [f"/opt/localize-guardian/bin/check-{index}", *("arg",) * 255]
        for index in range(64)
    ]
    focused_commands[0][-1] = exact_string
    prevention.update(
        {
            "allowed_code_path_globs": [
                f"localize/generated_{index}.py" for index in range(100)
            ],
            "allowed_test_path_globs": [
                f"tests/generated_{index}.py" for index in range(100)
            ],
            "focused_test_argv": focused_commands,
            "sandbox_argv_prefix": [
                "/opt/localize-guardian/bin/sandbox-wrapper",
            ],
            "max_changed_files": 100,
        }
    )
    prevention["push_repository"]["branch_prefix"] = "g/" + "a" * 176

    config = parse_guardian_config(raw)
    parsed = config.repositories[0].prevention

    assert parsed is not None
    assert len(parsed.allowed_code_path_globs) == 100
    assert len(parsed.allowed_test_path_globs) == 100
    assert len(parsed.focused_test_argv) == 64
    assert all(len(argv) == 256 for argv in parsed.focused_test_argv)
    assert parsed.sandbox_argv_prefix == (
        "/opt/localize-guardian/bin/sandbox-wrapper",
    )
    assert len(parsed.focused_test_argv[0][-1].encode("utf-8")) == 4096
    assert len(parsed.push_branch_prefix) + 77 == 255
    assert parsed.max_changed_files == 100


@pytest.mark.parametrize(
    ("case", "offending"),
    [
        ("code_globs", "allowed_code_path_globs"),
        ("test_globs", "allowed_test_path_globs"),
        ("focused_commands", "focused_test_argv"),
        ("focused_argv", "focused_test_argv"),
        ("sandbox_argv", "sandbox_argv_prefix"),
        ("utf8_string", "focused_test_argv"),
        ("changed_files", "max_changed_files"),
        ("branch_prefix", "branch_prefix"),
    ],
)
def test_rejects_prevention_values_above_durable_runtime_bounds(
    case: str,
    offending: str,
) -> None:
    raw = _raw_config_with_prevention()
    prevention = raw["repositories"][0]["prevention"]
    if case == "code_globs":
        prevention["allowed_code_path_globs"] = [
            f"localize/generated_{index}.py" for index in range(101)
        ]
    elif case == "test_globs":
        prevention["allowed_test_path_globs"] = [
            f"tests/generated_{index}.py" for index in range(101)
        ]
    elif case == "focused_commands":
        prevention["focused_test_argv"] = [
            [f"/opt/localize-guardian/bin/check-{index}"] for index in range(65)
        ]
    elif case == "focused_argv":
        prevention["focused_test_argv"] = [
            ["/opt/localize-guardian/bin/pytest", *("arg",) * 256]
        ]
    elif case == "sandbox_argv":
        prevention["sandbox_argv_prefix"] = [
            "/opt/localize-guardian/bin/sandbox-wrapper",
            "/unchecked/policy",
        ]
    elif case == "utf8_string":
        prevention["focused_test_argv"] = [
            ["/opt/localize-guardian/bin/pytest", "é" * 2049]
        ]
    elif case == "changed_files":
        prevention["max_changed_files"] = 101
    else:
        prevention["push_repository"]["branch_prefix"] = "g/" + "a" * 177

    with pytest.raises(GuardianConfigError, match=offending):
        parse_guardian_config(raw)


@pytest.mark.parametrize(
    "old, new, offending",
    [
        (
            "- [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]",
            "- bash -c pytest",
            "focused_test_argv",
        ),
        (
            "- [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]",
            "- [/bin/bash, -c, pytest]",
            "shell wrapper",
        ),
        (
            "- [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]",
            "- [/usr/bin/python, -c, print(1)]",
            "command string",
        ),
        (
            "- [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]",
            "- [/usr/bin/env, SAFE=value, pytest]",
            "shell wrapper",
        ),
        (
            "- [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]",
            "- [/opt/localize-guardian/bin/pytest, --api-key=not-allowed]",
            "credential",
        ),
        (
            "- [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]",
            "- [venv/bin/pytest, tests/unit/test_rules.py, -q]",
            "focused_test_argv.0.0",
        ),
        (
            "- /opt/localize-guardian/bin/sandbox-wrapper",
            "- sandbox-exec",
            "sandbox_argv_prefix.0",
        ),
        (
            "- /opt/localize-guardian/bin/sandbox-wrapper",
            "- /bin/sh",
            "shell wrapper",
        ),
        (
            "- /opt/localize-guardian/bin/sandbox-wrapper",
            "- TOKEN=not-allowed",
            "sandbox_argv_prefix.0",
        ),
    ],
)
def test_rejects_strings_shells_and_credentials_in_prevention_argv(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    yaml_text = _config_with_prevention().replace(old, new, 1)

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, yaml_text))


@pytest.mark.parametrize(
    "old, new, offending",
    [
        (
            '        - "localize/**/*.py"',
            '        - "localize/**/*.py"\n        - "localize/**/*.py"',
            "allowed_code_path_globs",
        ),
        (
            '        - "tests/**/*.py"',
            '        - "tests/**/*.py"\n        - "localize/**/*.py"',
            "code and test",
        ),
        (
            "        - [/opt/localize-guardian/bin/ruff, check, localize, tests]",
            """        - [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]""",
            "duplicate focused test argv",
        ),
    ],
)
def test_rejects_duplicate_prevention_globs_and_test_commands(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    yaml_text = _config_with_prevention().replace(old, new, 1)

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, yaml_text))


def test_rejects_ambiguous_prevention_repository_identities(tmp_path: Path) -> None:
    same_name_different_id = _config_with_prevention().replace(
        "full_name: localize-bot/localization-pipeline\n        id: 502",
        "full_name: acme/localization-pipeline\n        id: 502",
        1,
    )
    with pytest.raises(GuardianConfigError, match="repository identity"):
        load_guardian_config(_write_config(tmp_path, same_name_different_id))

    same_id_different_name = _config_with_prevention().replace(
        "id: 502",
        "id: 501",
        1,
    )
    with pytest.raises(GuardianConfigError, match="repository identity"):
        load_guardian_config(_write_config(tmp_path, same_id_different_name))


def test_reviewer_allowlists_are_scoped_by_repository_and_locale(tmp_path: Path) -> None:
    config_text = _minimal_config() + """
  - base_repo: example/translations
    base_repo_id: 84
    base_branch: main
    allowed_pr_authors:
      - {login: translation-service, id: 31, type: User}
    allowed_head_owners:
      - {login: example, id: 32, type: Organization}
    allowed_head_repositories:
      - {full_name: translation-service/translations, id: 168}
    allowed_branch_globs: ["translate/**"]
    allowed_path_globs: ["apps/**/i18n/**"]
    pipeline_config_path: config.yaml
    source_locale: en
    trusted_reviewers:
      de:
        - {login: second-maintainer, id: 303, type: User}
    trusted_bots:
      de:
        - {login: "coderabbitai[bot]", id: 404, type: Bot}
"""

    config = load_guardian_config(_write_config(tmp_path, config_text))

    first, second = config.repositories
    assert first.trusted_reviewers_for("ru")[0].id == 101
    assert first.trusted_reviewers_for("de") == ()
    assert second.trusted_reviewers_for("de")[0].id == 303
    assert second.trusted_reviewers_for("ru") == ()
    assert first.trusted_bots_for("ru")[0].id == 202
    assert second.trusted_bots_for("de")[0].id == 404


def test_actor_authorization_uses_immutable_numeric_id_not_login(tmp_path: Path) -> None:
    policy = load_guardian_config(
        _write_config(tmp_path, _minimal_config())
    ).repositories[0]

    trusted = policy.trusted_reviewer_by_id("ru", 101)
    assert trusted is not None
    assert trusted.login == "locale-maintainer"
    assert policy.trusted_reviewer_by_id("ru", 999) is None
    assert policy.trusted_bot_by_id("ru", 202) is not None
    assert policy.trusted_bot_by_id("ru", 101) is None
    assert policy.allowed_pr_author_by_id(999) is None
    assert policy.allowed_head_owner_by_id(999) is None


@pytest.mark.parametrize(
    "old, new, offending",
    [
        (
            "allowed_pr_authors:\n      - {login: localize-bot, id: 11, type: User}",
            "allowed_pr_authors: [localize-bot]",
            "allowed_pr_authors",
        ),
        (
            "allowed_head_owners:\n      - {login: acme, id: 12, type: Organization}",
            "allowed_head_owners: [acme]",
            "allowed_head_owners",
        ),
        (
            "    base_repo_id: 42\n",
            "",
            "base_repo_id",
        ),
        (
            "    allowed_head_repositories:\n      - {full_name: localize-bot/widgets, id: 84}\n",
            "",
            "allowed_head_repositories",
        ),
        (
            "{login: localize-bot, id: 11, type: User}",
            "{login: localize-bot, id: 11, type: Organization}",
            "allowed_pr_authors",
        ),
    ],
)
def test_owned_pr_policy_requires_numeric_typed_identities(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, _minimal_config().replace(old, new)))


@pytest.mark.parametrize(
    "old, new, offending",
    [
        (
            "- {login: locale-maintainer, id: 101, type: User}",
            "- locale-maintainer",
            "trusted_reviewers",
        ),
        (
            "- {login: locale-maintainer, id: 101, type: User}",
            "- {login: locale-maintainer, id: 101, type: Bot}",
            "User",
        ),
        (
            '- {login: "coderabbitai[bot]", id: 202, type: Bot}',
            '- {login: "coderabbitai[bot]", id: 202, type: User}',
            "Bot",
        ),
        (
            "- {login: locale-maintainer, id: 101, type: User}",
            """- {login: locale-maintainer, id: 101, type: User}
        - {login: renamed-maintainer, id: 101, type: User}""",
            "duplicate actor id",
        ),
        (
            '- {login: "coderabbitai[bot]", id: 202, type: Bot}',
            '- {login: "coderabbitai[bot]", id: 101, type: Bot}',
            "duplicate actor id",
        ),
    ],
)
def test_rejects_login_only_wrong_type_and_duplicate_actor_ids(
    tmp_path: Path,
    old: str,
    new: str,
    offending: str,
) -> None:
    yaml_text = _minimal_config().replace(old, new)

    with pytest.raises(GuardianConfigError, match=offending):
        load_guardian_config(_write_config(tmp_path, yaml_text))


@pytest.mark.parametrize(
    "replacement",
    [
        ("acme/widgets", "acme"),
        (".localize/config.yaml", "/tmp/config.yaml"),
        (".localize/config.yaml", "../config.yaml"),
    ],
)
def test_rejects_invalid_repository_or_unsafe_config_paths(
    tmp_path: Path,
    replacement: tuple[str, str],
) -> None:
    old, new = replacement
    yaml_text = _minimal_config().replace(old, new)

    with pytest.raises(GuardianConfigError):
        load_guardian_config(_write_config(tmp_path, yaml_text))


def test_stable_replacement_model_carries_review_evidence() -> None:
    replacement = ProposedReplacement(
        feedback_id="review-comment:42",
        path="resources/l10n/messages_ru.properties",
        key="Push_to_%0_was_rejected_(%1)._%2_%3",
        locale="ru",
        expected_value="old",
        proposed_value="new",
        source_value="Push to %0 was rejected (%1). %2 %3",
        confidence=0.99,
        evidence=("Maintainer supplied the correction.",),
    )

    assert replacement.feedback_id == "review-comment:42"
    assert replacement.source_value == "Push to %0 was rejected (%1). %2 %3"
    assert replacement.evidence == ("Maintainer supplied the correction.",)
