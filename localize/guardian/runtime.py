"""Production-only assembly for one bounded Localize Guardian poll.

The core controller is dependency-injected and fully testable without network
or credential access.  This module is the deliberately small trust boundary
that connects it to GitHub, Codex, ephemeral Git workspaces, and private local
state.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import os
from pathlib import Path
import stat
from typing import Any

import httpx

from localize.guardian.codex import CodexDriver
from localize.guardian.config import GuardianConfigError, parse_guardian_config_yaml
from localize.guardian.controller import GuardianController, PollOutcome
from localize.guardian.credentials import (
    CredentialError,
    SecretCommand,
    git_credential_environment,
    resolve_model_api_key,
)
from localize.guardian.executable_trust import (
    ExecutableTrustError,
    require_absolute_trusted_executable,
)
from localize.guardian.github import (
    FeedbackRevision,
    GitHubAuthenticationError,
    GitHubReader,
    GitHubRepositoryPolicy,
    GitHubWriteBroker,
    PullRequestFeedbackSnapshot,
)
from localize.guardian.models import (
    CodexAuthMode,
    GuardianConfig,
    GuardianMode,
    PreventionPolicy,
    RepositoryPolicy,
)
from localize.guardian.prevention_runtime import (
    PreventionCodexAuthor,
    PreventionCoordinator,
    PreventionGitHubBroker,
    SandboxedTestRunner,
)
from localize.guardian.scheduler import is_run_due
from localize.guardian.signing import canonical_signing_key
from localize.guardian.state import GuardianState
from localize.guardian.workspace import ExactRevision, materialize_exact_checkout


_GITHUB_API_URL = "https://api.github.com"
_GITHUB_HOST = "github.com"
_POLL_ATTEMPT_COMPONENT = "guardian-poll-attempt"
_MAX_CONFIG_BYTES = 1_048_576
_MAX_CODEX_AUTH_BYTES = 1_048_576
_WRITE_MODES = frozenset(
    {
        GuardianMode.APPLY_OWNED_TRANSLATIONS,
        GuardianMode.PROPOSE_PREVENTION,
    }
)


class GuardianRuntimeError(RuntimeError):
    """A redacted production-wiring failure safe for operator output."""


def _resolved_config_path(path: str | Path) -> Path:
    """Return an absolute path without silently following a symlinked leaf."""

    return Path(os.path.abspath(Path(path).expanduser()))


def _permission_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _trusted_owners() -> frozenset[int]:
    owners = {0}
    if hasattr(os, "getuid"):
        owners.add(os.getuid())
    return frozenset(owners)


def _validate_trusted_ancestors(path: Path) -> None:
    owners = _trusted_owners()
    for ancestor in reversed(path.parents):
        try:
            metadata = ancestor.stat(follow_symlinks=False)
        except OSError:
            raise GuardianRuntimeError(
                "Guardian configuration is unavailable or unsafe."
            ) from None
        if (
            ancestor.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in owners
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise GuardianRuntimeError(
                "Guardian configuration is unavailable or unsafe."
            )


def load_trusted_guardian_config(path: Path) -> GuardianConfig:
    """Parse the exact regular file verified through one non-following descriptor."""

    _validate_trusted_ancestors(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise GuardianRuntimeError(
            "Guardian configuration is unavailable or unsafe."
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise GuardianRuntimeError(
                "Guardian configuration is unavailable or unsafe."
            )
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
            raw_text = source.read(_MAX_CONFIG_BYTES + 1)
        if len(raw_text.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise GuardianRuntimeError(
                "Guardian configuration is unavailable or unsafe."
            )
    except (OSError, UnicodeError):
        raise GuardianRuntimeError(
            "Guardian configuration is unavailable or unsafe."
        ) from None
    finally:
        os.close(descriptor)
    try:
        return parse_guardian_config_yaml(raw_text)
    except GuardianConfigError:
        raise GuardianRuntimeError("Guardian configuration is invalid.") from None


def _private_state_paths(config_path: Path) -> tuple[Path, Path]:
    directory = config_path.parent / ".guardian"
    return directory, directory / "state.sqlite3"


def _prepare_private_state(config_path: Path) -> tuple[Path, Path]:
    """Create or validate the non-shared Guardian state boundary."""

    directory, database = _private_state_paths(config_path)
    try:
        if directory.is_symlink():
            raise GuardianRuntimeError("Guardian state path must remain private.")
        if directory.exists():
            metadata = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or _permission_bits(directory) != 0o700:
                raise GuardianRuntimeError("Guardian state path must remain private.")
        else:
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)

        if database.is_symlink():
            raise GuardianRuntimeError("Guardian state path must remain private.")
        if database.exists():
            metadata = database.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or _permission_bits(database) != 0o600:
                raise GuardianRuntimeError("Guardian state path must remain private.")
    except GuardianRuntimeError:
        raise
    except OSError:
        raise GuardianRuntimeError("Guardian state path must remain private.") from None
    return directory, database


def _resolved_codex_home(config: GuardianConfig) -> Path:
    return Path(os.path.abspath(Path(config.runtime.codex_home).expanduser()))


def _validate_subscription_codex_home(config: GuardianConfig) -> Path:
    """Require one private file-backed ChatGPT login owned by the operator."""

    codex_home = _resolved_codex_home(config)
    auth_file = codex_home / "auth.json"
    try:
        _validate_trusted_ancestors(codex_home)
        home_metadata = codex_home.stat(follow_symlinks=False)
        auth_metadata = auth_file.stat(follow_symlinks=False)
        if (
            codex_home.is_symlink()
            or not stat.S_ISDIR(home_metadata.st_mode)
            or (hasattr(os, "getuid") and home_metadata.st_uid != os.getuid())
            or stat.S_IMODE(home_metadata.st_mode) != 0o700
            or auth_file.is_symlink()
            or not stat.S_ISREG(auth_metadata.st_mode)
            or (hasattr(os, "getuid") and auth_metadata.st_uid != os.getuid())
            or stat.S_IMODE(auth_metadata.st_mode) != 0o600
            or auth_metadata.st_size <= 0
            or auth_metadata.st_size > _MAX_CODEX_AUTH_BYTES
        ):
            raise GuardianRuntimeError(
                "Guardian ChatGPT authentication is unavailable or unsafe."
            )
    except GuardianRuntimeError:
        raise
    except OSError:
        raise GuardianRuntimeError(
            "Guardian ChatGPT authentication is unavailable or unsafe."
        ) from None
    return codex_home


def _validate_scheduled_executables(config: GuardianConfig) -> None:
    commands: list[tuple[Sequence[str], str]] = [
        ((config.runtime.codex_executable,), "runtime.codex_executable"),
        ((config.runtime.git_executable,), "runtime.git_executable"),
        (config.runtime.github_token_command, "runtime.github_token_command"),
    ]
    if config.mode in _WRITE_MODES:
        commands.append(
            ((config.runtime.signing_program,), "runtime.signing_program")
        )
    if config.runtime.codex_auth_mode is CodexAuthMode.API_KEY:
        commands.append(
            (config.runtime.codex_api_key_command, "runtime.codex_api_key_command")
        )
    if config.mode is GuardianMode.PROPOSE_PREVENTION:
        for policy_index, policy in enumerate(config.repositories):
            prevention = policy.prevention
            if prevention is None:
                continue
            commands.append(
                (
                    prevention.sandbox_argv_prefix,
                    f"repositories.{policy_index}.prevention.sandbox_argv_prefix",
                )
            )
            commands.extend(
                (argv, f"repositories.{policy_index}.prevention.focused_test_argv")
                for argv in prevention.focused_test_argv
            )
    try:
        for command, field in commands:
            require_absolute_trusted_executable(command, field=field)
    except ExecutableTrustError:
        raise GuardianRuntimeError(
            "Guardian scheduled executable authority is unavailable or unsafe."
        ) from None


def _validate_runtime_authority(config: GuardianConfig, *, scheduled: bool) -> None:
    if config.runtime.codex_auth_mode is CodexAuthMode.CHATGPT:
        _validate_subscription_codex_home(config)
    if scheduled:
        _validate_scheduled_executables(config)


def _github_policy(policy: RepositoryPolicy) -> GitHubRepositoryPolicy:
    return GitHubRepositoryPolicy(
        repository=policy.base_repo,
        repository_id=policy.base_repo_id,
        base_branch=policy.base_branch,
        allowed_pr_authors=policy.allowed_pr_authors,
        allowed_head_owners=policy.allowed_head_owners,
        allowed_head_repositories=policy.allowed_head_repositories,
        branch_globs=policy.allowed_branch_globs,
    )


class AuthenticatedGitHubSnapshotProvider:
    """Mint a read credential and collect complete GitHub feedback snapshots."""

    def __init__(
        self,
        *,
        credential: SecretCommand,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("GitHub timeout must be positive.")
        self._credential = credential
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    def __call__(
        self,
        policy: RepositoryPolicy,
        previous_feedback: tuple[FeedbackRevision, ...],
    ) -> Sequence[PullRequestFeedbackSnapshot]:
        try:
            token = self._credential.read()
        except CredentialError:
            raise GitHubAuthenticationError(
                "GitHub credential helper failed"
            ) from None
        try:
            with httpx.Client(
                base_url=_GITHUB_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "localize-guardian",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                return GitHubReader(
                    client,
                    _github_policy(policy),
                ).collect_open_pull_requests(previous_feedback=previous_feedback)
        finally:
            token = ""


def _attempt_timeout(config: GuardianConfig) -> float:
    """Keep configured retries within one model-call timeout budget."""

    return config.limits.run_timeout_seconds / config.limits.max_attempts


def _require_explicit_write_signing_key(config: GuardianConfig) -> None:
    key = config.runtime.signing_key
    if config.mode not in _WRITE_MODES:
        return
    try:
        canonical_signing_key(key)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise GuardianRuntimeError(
            "Guardian write modes require an explicit full signing key fingerprint."
        ) from None


def _build_controller(
    *,
    config: GuardianConfig,
    state: GuardianState,
    state_directory: Path,
    github_credential: SecretCommand,
    model_credential: SecretCommand | None,
    git_environment: Any,
) -> GuardianController:
    """Assemble trusted production adapters without invoking a credential yet."""

    _require_explicit_write_signing_key(config)
    attempt_timeout = _attempt_timeout(config)
    github_timeout = min(30.0, attempt_timeout)
    snapshot_provider = AuthenticatedGitHubSnapshotProvider(
        credential=github_credential,
        timeout_seconds=github_timeout,
    )
    codex_driver = CodexDriver(
        model=config.runtime.codex_model,
        reasoning_effort=config.runtime.codex_reasoning_effort,
        auth_mode=config.runtime.codex_auth_mode,
        codex_home=config.runtime.codex_home,
        executable=config.runtime.codex_executable,
        timeout_seconds=attempt_timeout,
        max_attempts=config.limits.max_attempts,
    )

    def checkout_factory(revision: ExactRevision):
        return materialize_exact_checkout(
            revision,
            credential_environment=git_environment,
            git_binary=config.runtime.git_executable,
            signing_program=config.runtime.signing_program,
            timeout_seconds=attempt_timeout,
        )

    def model_credential_provider() -> str | None:
        if config.runtime.codex_auth_mode is CodexAuthMode.CHATGPT:
            return None
        return resolve_model_api_key(model_credential)

    write_broker_factory = None
    if config.mode in _WRITE_MODES:

        def create_write_broker(policy: RepositoryPolicy) -> GitHubWriteBroker:
            return GitHubWriteBroker(
                policy=_github_policy(policy),
                token_command=config.runtime.github_token_command,
                base_url=_GITHUB_API_URL,
                timeout=github_timeout,
                token_command_timeout=github_timeout,
            )

        write_broker_factory = create_write_broker

    prevention_runner = None
    if config.mode is GuardianMode.PROPOSE_PREVENTION:

        def create_prevention_broker(
            policy: PreventionPolicy,
        ) -> PreventionGitHubBroker:
            return PreventionGitHubBroker(
                policy=policy,
                token_command=config.runtime.github_token_command,
                github_host=_GITHUB_HOST,
                base_url=_GITHUB_API_URL,
                timeout_seconds=github_timeout,
                token_command_timeout=github_timeout,
            )

        prevention_runner = PreventionCoordinator(
            state=state,
            checkout_factory=checkout_factory,
            broker_factory=create_prevention_broker,
            author=PreventionCodexAuthor(
                model=config.runtime.codex_model,
                reasoning_effort=config.runtime.codex_reasoning_effort,
                auth_mode=config.runtime.codex_auth_mode,
                codex_home=config.runtime.codex_home,
                executable=config.runtime.codex_executable,
                timeout_seconds=attempt_timeout,
                max_attempts=config.limits.max_attempts,
            ),
            test_runner=SandboxedTestRunner(timeout_seconds=attempt_timeout),
            model_credential_provider=model_credential_provider,
            publish_credential_environment=git_environment,
            signing_key=config.runtime.signing_key,
            signing_environment=None,
            max_drafts=config.limits.max_prevention_drafts_per_run,
            reservation_usd=config.limits.model_call_reservation_usd,
            daily_limit_usd=config.limits.daily_cost_limit_usd,
            max_model_calls_per_day=config.limits.max_model_calls_per_day,
            api_billed=(
                config.runtime.codex_auth_mode is CodexAuthMode.API_KEY
            ),
            temporary_root=state_directory,
        )

    return GuardianController(
        config=config,
        state=state,
        snapshot_provider=snapshot_provider,
        checkout_factory=checkout_factory,
        codex_driver=codex_driver,
        model_credential_provider=model_credential_provider,
        write_broker_factory=write_broker_factory,
        prevention_runner=prevention_runner,
        publish_credential_environment=git_environment,
        evidence_root=state_directory / "evidence",
        github_host=_GITHUB_HOST,
        signing_key=config.runtime.signing_key,
    )


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _scheduled_poll_is_due(state: GuardianState, *, now: datetime) -> bool:
    """Use the latest durable poll attempt as the once-daily checkpoint."""

    latest_attempt = state.latest_health(_POLL_ATTEMPT_COMPONENT)
    if latest_attempt is None:
        # Upgrade compatibility: older Guardian versions recorded only a
        # successful bounded poll. Respect that same-day checkpoint once.
        legacy_success = state.latest_health("guardian")
        latest_attempt = (
            legacy_success
            if legacy_success is not None and legacy_success.status == "ok"
            else None
        )
    return is_run_due(
        now=now,
        last_success=(
            latest_attempt.checked_at if latest_attempt is not None else None
        ),
        hour=0,
        minute=0,
    )


def _exit_code(outcome: PollOutcome) -> int:
    if (
        outcome.authentication_circuit_open
        or outcome.model_circuit_open
        or outcome.runs_failed
        or outcome.failures
    ):
        return 1
    return 0


def run_once(config_path: Path, scheduled: bool = False) -> int:
    """Load trusted policy and execute one finite Guardian poll.

    Scheduled calls run at most once per local calendar day after the first
    attempted wake, regardless of its outcome. Manual invocations always poll.
    Credential contents and
    untrusted exception messages never cross this adapter's error boundary.
    """

    if not isinstance(scheduled, bool):
        raise GuardianRuntimeError("Guardian scheduled flag is invalid.")
    resolved_config = _resolved_config_path(config_path)
    config = load_trusted_guardian_config(resolved_config)

    _require_explicit_write_signing_key(config)

    state_directory, state_path = _prepare_private_state(resolved_config)
    try:
        state_context = GuardianState(state_path)
    except Exception:
        raise GuardianRuntimeError("Guardian private state is unavailable.") from None

    try:
        with state_context as state:
            attempted_at = _local_now()
            if scheduled and not _scheduled_poll_is_due(state, now=attempted_at):
                return 0
            state.record_health(
                component=_POLL_ATTEMPT_COMPONENT,
                status="attempted",
                message="Guardian poll attempt started.",
                details={"scheduled": scheduled},
                checked_at=attempted_at,
            )
            _validate_runtime_authority(config, scheduled=scheduled)

            helper_timeout = min(30.0, _attempt_timeout(config))
            github_credential = SecretCommand(
                config.runtime.github_token_command,
                timeout_seconds=helper_timeout,
            )
            model_credential = (
                SecretCommand(
                    config.runtime.codex_api_key_command,
                    timeout_seconds=helper_timeout,
                )
                if config.runtime.codex_auth_mode is CodexAuthMode.API_KEY
                else None
            )
            with git_credential_environment(
                github_credential,
                temporary_root=state_directory,
            ) as git_environment:
                controller = _build_controller(
                    config=config,
                    state=state,
                    state_directory=state_directory,
                    github_credential=github_credential,
                    model_credential=model_credential,
                    git_environment=git_environment,
                )
                try:
                    outcome = controller.poll_once()
                except Exception:
                    raise GuardianRuntimeError(
                        "Guardian poll failed before a bounded outcome was recorded."
                    ) from None
                return _exit_code(outcome)
    except GuardianRuntimeError:
        raise
    except Exception:
        raise GuardianRuntimeError("Guardian production runtime failed safely.") from None


__all__: Sequence[str] = (
    "AuthenticatedGitHubSnapshotProvider",
    "GuardianRuntimeError",
    "load_trusted_guardian_config",
    "run_once",
)
