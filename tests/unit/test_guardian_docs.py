"""Static safety and portability checks for the public Guardian guide."""

from __future__ import annotations

from pathlib import Path

import yaml

from localize.guardian.config import load_guardian_config
from localize.guardian.models import CodexAuthMode, GuardianMode, PipelineConfigSource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUIDE = PROJECT_ROOT / "docs" / "guardian.md"
EXAMPLE = PROJECT_ROOT / "examples" / "guardian.config.yaml"
README = PROJECT_ROOT / "README.md"
LLMS = PROJECT_ROOT / "llms.txt"


def _guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _example_text() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def _normalized_guide() -> str:
    return " ".join(_guide_text().casefold().split())


def test_guardian_example_is_valid_report_only_policy_with_numeric_identities():
    config = load_guardian_config(EXAMPLE)

    assert config.mode is GuardianMode.OBSERVE
    assert len(config.repositories) == 1
    policy = config.repositories[0]
    assert policy.base_repo == "acme/widgets"
    assert policy.base_repo_id > 0
    assert policy.base_branch == "main"
    assert policy.private_repo_model_opt_in is False
    assert config.runtime.codex_model == "gpt-5.6-terra"
    assert config.runtime.codex_reasoning_effort == "high"
    assert config.runtime.codex_auth_mode is CodexAuthMode.CHATGPT
    assert config.runtime.codex_home == "~/.local/share/localize-guardian/codex"
    assert config.runtime.codex_api_key_command == ()
    assert config.limits.max_model_calls_per_day == 2
    assert config.limits.daily_cost_limit_usd is None
    assert config.limits.model_call_reservation_usd is None
    assert config.schedule.hour == 0
    assert config.schedule.minute == 0
    assert policy.pipeline_config_source is PipelineConfigSource.BASE

    assert policy.allowed_pr_authors
    assert policy.allowed_head_owners
    assert policy.allowed_head_repositories
    assert all(actor.id > 0 for actor in policy.allowed_pr_authors)
    assert all(actor.id > 0 for actor in policy.allowed_head_owners)
    assert all(repository.id > 0 for repository in policy.allowed_head_repositories)

    reviewers = policy.trusted_reviewers_for("de")
    bots = policy.trusted_bots_for("de")
    assert reviewers and bots
    assert all(actor.id > 0 and actor.type == "User" for actor in reviewers)
    assert all(actor.id > 0 and actor.type == "Bot" for actor in bots)
    assert {actor.id for actor in reviewers}.isdisjoint(actor.id for actor in bots)


def test_guardian_example_contains_policy_not_credentials():
    raw = yaml.safe_load(_example_text())
    assert isinstance(raw, dict)

    forbidden_key_fragments = ("password", "secret", "token", "api_key", "credential")

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold()
                assert not any(part in normalized for part in forbidden_key_fragments)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(raw)
    text = _example_text()
    assert "inert placeholder" in text.casefold()
    assert "OPENAI_API_KEY" not in text
    assert "GITHUB_TOKEN" not in text
    assert "ghp_" not in text


def test_guardian_guide_covers_operator_ownership_and_authority_modes():
    guide = _guide_text()
    normalized = _normalized_guide()

    assert "operator runs" in normalized
    assert "supplies their own codex/chatgpt plan" in normalized
    assert "explicitly opts into api billing" in normalized
    assert "not a hosted service" in normalized
    for mode in (
        "observe",
        "prepare",
        "apply-owned-translations",
        "propose-prevention",
    ):
        assert f"`{mode}`" in guide
    assert "report-only" in normalized
    assert "cannot raise" in normalized
    assert "creates no commits, pushes, comments, or other github writes" in normalized
    assert "stores the outcome and a changed-key count in private action state" in normalized
    assert "retains no patch or reviewable plan" in normalized
    assert "not the prepared key count or a diff" in normalized
    assert "do not treat `prepare` as a reviewable diff preview" in normalized


def test_guardian_guide_documents_identity_privacy_and_write_boundaries():
    guide = _guide_text()
    lowered = _normalized_guide()

    assert "numeric github id" in lowered
    assert "base_repo_id" in guide
    assert "exact configured target base branch (`base_branch`)" in lowered
    assert "allowed_head_repositories[].id" in guide
    assert "are authoritative" in lowered
    assert "per repository and locale" in lowered
    assert "trusted_reviewers" in guide
    assert "trusted_bots" in guide
    assert "may authorize auto-application" in lowered
    assert "never inherits trust" in lowered
    assert "allowed_head_repositories" in _example_text()
    assert "private_repo_model_opt_in" in guide
    assert "explicit opt-in" in lowered
    assert "value-only" in lowered
    assert "expected value" in lowered
    assert "source value" in lowered
    assert "exact trusted base sha" in lowered
    assert "never from the pr head" in lowered
    assert "head sha" in lowered
    assert "base sha" in lowered
    assert "does not merge" in lowered
    assert "resolve review threads" in lowered
    assert "`pipeline_config_source: operator`" in lowered
    assert "snapshot" in lowered
    assert "mode `0700`" in lowered
    assert "mode `0600`" in lowered
    assert "exact base sha" in lowered


def test_guardian_guide_documents_hardened_codex_boundary():
    guide = _guide_text()
    normalized = _normalized_guide()

    assert "Codex CLI" in guide
    assert "codex exec" in guide
    assert "`guardian_evidence` permission profile" in guide
    assert "minimal filesystem reads plus read access" in normalized
    for option in (
        "--ephemeral",
        "--output-schema",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--ask-for-approval never",
    ):
        assert f"`{option}`" in guide
    assert "https://learn.chatgpt.com/docs/non-interactive-mode" in guide

    forbidden = (
        "bypassPermissions",
        "danger-full-access",
        "dangerously-bypass",
        "--full-auto",
        "--sandbox read-only",
    )
    assert not any(value in guide for value in forbidden)
    assert "`gpt-5.6-terra` with reasoning effort `high`" in guide


def test_guardian_guide_documents_deliberate_plugin_isolation():
    guide = _guide_text()

    assert "An explicit `--plugin` argument is rejected" in guide
    assert "`LOCALIZE_PLUGIN_MODULES`" in guide
    assert "entry points" in guide
    assert "fails closed" in guide


def test_guardian_guide_documents_untrusted_inputs_and_secret_brokerage():
    guide = _guide_text()
    lowered = _normalized_guide()

    for untrusted_input in (
        "review comments",
        "repository content",
        "model output",
    ):
        assert untrusted_input in lowered
    assert "prompt injection" in lowered
    assert "os secret store" in lowered
    assert "token helper" in lowered
    assert "never stored in guardian yaml" in lowered
    assert "shell=false" in lowered
    assert "`codex_auth_mode: chatgpt`" in lowered
    assert "localize guardian login --config" in guide
    assert "dedicated `codex_home`" in lowered
    assert "chatgpt plan allowance" in lowered
    assert "not metered api billing" in lowered
    assert "forced_login_method=\"chatgpt\"" in guide
    assert "cli_auth_credentials_store=\"file\"" in guide
    assert "`codex_api_key` and `openai_api_key` are removed" in lowered
    assert "api-key mode is an explicit opt-in" in lowered
    assert "does not accept an ambient api key" in lowered
    assert "opens the poll's authentication circuit" in lowered
    assert "model-capacity circuit" in lowered
    assert "does not immediately retry" in lowered
    assert "github credential-helper or api authentication failure" in lowered
    assert "non-authentication github transport" in lowered


def test_guardian_guide_documents_audit_cost_retention_and_safe_prevention():
    guide = _guide_text()
    lowered = _normalized_guide()

    assert "immutable revision" in lowered
    assert "body hash" in lowered
    assert "daily model-call" in lowered
    assert "raw_retention_days" in guide
    assert "logically deleted from the active sqlite tables" in lowered
    assert "not a secure-erasure guarantee" in lowered
    assert "draft pull request" in lowered
    assert "regression test" in lowered
    assert "failing on the base" in lowered
    assert "passing with the draft" in lowered
    assert "operator-supplied `sandbox_argv_prefix`" in lowered
    assert "its executable must be an absolute path" in lowered
    assert "before every focused command, a runtime probe" in lowered
    assert "an af_inet loopback bind and a connection" in lowered
    assert "filesystem af_unix canary connection" in lowered
    assert "does not prove the policy's behavior for every host path" in lowered
    assert "parent guardian process must be allowed" in lowered
    assert "operator-controlled absolute interpreter" in lowered
    assert "every block must state `private_target_model_opt_in`" in lowered
    assert "both opt-ins are required" in lowered
    assert "non-refundable per-poll publication slot" in lowered
    assert "across the entire poll, shared by all repositories" in lowered
    assert "counter is not reset per feedback run" in lowered
    assert "a zero cap is a prevention-publication kill switch" in lowered
    assert "still retains the translation-write authority" in lowered
    assert "not whole-run exactly-once execution" in lowered
    assert "can repeat an ambiguous model attempt" in lowered
    assert "consume another call slot" in lowered
    assert "max_model_calls_per_day >= max_attempts" in guide
    assert "max_attempts *" in guide
    assert "(1 + max_prevention_drafts_per_run)" in guide
    assert "daily_cost_limit_usd >= model_call_reservation_usd" in guide
    assert "report-only example pins the cap to zero" in lowered
    assert "cap provides two full" in _example_text()
    assert "[/absolute/path/to/python, -m, pytest" in _example_text()
    assert "private_target_model_opt_in: false" in _example_text()
    assert "🤖" in guide
    assert "commit-linked" in lowered


def test_guardian_guide_has_consistent_cli_and_launchd_catch_up_instructions():
    guide = _guide_text()

    for command in ("init", "login", "doctor", "run", "status", "install"):
        assert f"localize guardian {command} --config" in guide
    assert "launchd" in guide
    assert "RunAtLoad" in guide
    assert "StartInterval" in guide
    assert "wake" in guide.casefold()
    assert "catch-up" in guide.casefold()
    assert "failed scheduled attempt is not retried" in guide.casefold()
    assert "schedule.hour" in guide
    assert "schedule.minute" in guide
    assert "local wall-clock" in guide.casefold()
    assert "explicit manual `guardian run`" in guide
    assert "stages the files but does not load" in guide
    assert "runtime.codex_executable" in guide
    assert "runtime.github_token_command[0]" in guide
    assert "runtime.codex_api_key_command[0]" in guide
    assert "executable absolute paths" in guide
    assert "removes only regular files created by that attempt" in guide
    assert "preserves pre-existing" in guide
    assert "not a file to commit" in _normalized_guide()

    example = _example_text()
    assert "codex_executable: /absolute/path/to/codex" in example
    assert "github_token_command: [/absolute/path/to/github-token-helper]" in example
    assert "codex_api_key_command: [/absolute/path/to/model-key-helper]" in example
    assert "codex_auth_mode: chatgpt" in example
    assert "max_model_calls_per_day: 2" in example


def test_guardian_is_discoverable_without_implying_a_hosted_service():
    readme = README.read_text(encoding="utf-8")
    llms = LLMS.read_text(encoding="utf-8")

    for text in (readme, llms):
        normalized = " ".join(text.split())
        assert "docs/guardian.md" in text
        assert "examples/guardian.config.yaml" in text
        assert "self-hosted" in text.casefold()
        assert "operator" in text.casefold()
        assert "gpt-5.6-terra" in text
        assert "LOCALIZE_PLUGIN_MODULES" in text
        assert "manual `guardian run`" in normalized
    assert "not a service run" in readme.casefold()
    assert "does not operate the guardian" in llms.casefold()


def test_guardian_public_files_stay_generic_and_have_no_project_runner():
    combined = f"{_guide_text()}\n{_example_text()}".casefold()

    assert "bisq" not in combined
    assert "jabref" not in combined
    assert "run-pilot" not in combined
    assert "outreach/" not in combined
    assert "profiles/" not in combined

    readme = README.read_text(encoding="utf-8")
    guardian_start = readme.index("## Optional Self-Hosted PR Guardian")
    guardian_end = readme.index("\n## ", guardian_start + 1)
    all_discovery_text = "\n".join(
        (
            combined,
            readme[guardian_start:guardian_end].casefold(),
            LLMS.read_text(encoding="utf-8").casefold(),
        )
    )
    assert "jabref" not in all_discovery_text
    assert "ultra" not in combined
    assert "ultra" not in readme[guardian_start:guardian_end].casefold()
    llms = LLMS.read_text(encoding="utf-8").casefold()
    llms_guardian_start = llms.index("guardian authority")
    llms_guardian = llms[
        llms_guardian_start : llms.index("\n## ", llms_guardian_start)
    ]
    assert "ultra" not in llms_guardian
