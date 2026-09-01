"""Tests for the PR guardian's strict, least-privilege configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from localize.guardian import (
    AllowedHeadRepository,
    CodexAuthMode,
    ExactRepository,
    GuardianMode,
    PipelineConfigSource,
    PreventionPolicy,
    ProposedReplacement,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.config import GuardianConfigError, load_guardian_config


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
    allowed_pr_authors:
      - {login: localize-bot, id: 11, type: Bot}
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
      allowed_code_path_globs:
        - "localize/**/*.py"
      allowed_test_path_globs:
        - "tests/**/*.py"
      focused_test_argv:
        - [/opt/localize-guardian/bin/pytest, tests/unit/test_rules.py, -q]
        - [/opt/localize-guardian/bin/ruff, check, localize, tests]
      sandbox_argv_prefix:
        - /usr/bin/sandbox-exec
        - -f
        - /Users/operator/.config/localize-guardian/test.sb
        - --
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


def test_minimal_config_is_report_only_with_safe_limits(tmp_path: Path) -> None:
    config = load_guardian_config(_write_config(tmp_path, _minimal_config()))

    assert config.mode is GuardianMode.OBSERVE
    assert config.report_only is True
    assert config.limits.run_timeout_seconds == 3600
    assert config.limits.max_attempts == 2
    assert config.limits.max_value_edits_per_run == 20
    assert config.limits.max_prevention_drafts_per_run == 1
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
    assert config.runtime.signing_program == "gpg"
    assert config.runtime.github_token_command == ("gh", "auth", "token")
    assert config.runtime.codex_api_key_command == ()
    assert config.runtime.signing_key is None
    assert config.schedule.hour == 0
    assert config.schedule.minute == 0

    policy = config.repositories[0]
    assert policy.base_repo == "acme/widgets"
    assert policy.base_repo_id == 42
    assert policy.base_branch == "main"
    assert policy.allowed_pr_author_by_id(11) == TrustedActor(
        login="localize-bot", id=11, type="Bot"
    )
    assert policy.allowed_head_owner_by_id(12) == TrustedActor(
        login="acme", id=12, type="Organization"
    )
    assert policy.allowed_head_repository_by_id(84).full_name == "localize-bot/widgets"
    assert policy.private_repo_model_opt_in is False
    assert policy.pipeline_config_source is PipelineConfigSource.BASE
    assert policy.prevention is None
    assert policy.trusted_reviewers_for("ru") == (
        TrustedActor(login="locale-maintainer", id=101, type="User"),
    )
    assert policy.trusted_bots_for("ru") == (
        TrustedActor(login="coderabbitai[bot]", id=202, type="Bot"),
    )
    assert policy.trusted_reviewers_for("de") == ()
    assert policy.trusted_bots_for("de") == ()


def test_repository_policy_preserves_legacy_positional_defaults() -> None:
    prevention = PreventionPolicy(
        target_repository=ExactRepository("acme/pipeline", 501),
        target_base_branch="main",
        push_repository=ExactRepository("localize-bot/pipeline", 502),
        push_branch_prefix="guardian/prevention-",
        allowed_code_path_globs=("localize/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(("/opt/bin/pytest", "tests/unit/test_rules.py"),),
        sandbox_argv_prefix=("/usr/bin/sandbox-exec", "--"),
        max_changed_files=4,
        max_changed_bytes=262_144,
    )

    policy = RepositoryPolicy(
        "acme/widgets",
        42,
        "main",
        (TrustedActor("localize-bot", 11, "Bot"),),
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
        sandbox_argv_prefix=(
            "/usr/bin/sandbox-exec",
            "-f",
            "/Users/operator/.config/localize-guardian/test.sb",
            "--",
        ),
        max_changed_files=4,
        max_changed_bytes=262144,
    )


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
  codex_api_key_command: [/usr/bin/security, find-generic-password, -w, -s, guardian]
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
        "/usr/bin/security",
        "find-generic-password",
        "-w",
        "-s",
        "guardian",
    )
    assert config.runtime.signing_key == fingerprint


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


@pytest.mark.parametrize("yaml_number", [".nan", ".inf", "-.inf"])
def test_rejects_non_finite_daily_cost_limits(
    tmp_path: Path,
    yaml_number: str,
) -> None:
    yaml_text = (
        f"limits:\n  daily_cost_limit_usd: {yaml_number}\n" + _minimal_config()
    )

    with pytest.raises(GuardianConfigError, match="finite"):
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
            "- /usr/bin/sandbox-exec",
            "- sandbox-exec",
            "sandbox_argv_prefix.0",
        ),
        (
            "- /usr/bin/sandbox-exec",
            "- /bin/sh",
            "shell wrapper",
        ),
        (
            "- /usr/bin/sandbox-exec",
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
            "allowed_pr_authors:\n      - {login: localize-bot, id: 11, type: Bot}",
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
            "{login: localize-bot, id: 11, type: Bot}",
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
