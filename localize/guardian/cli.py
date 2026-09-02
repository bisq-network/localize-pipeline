"""Operator-owned command surface for the self-hosted Localize Guardian."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Iterator, Sequence

from jsonschema import Draft202012Validator
import httpx

from localize.formats import list_localization_adapters
from localize.guardian.codex import (
    RESULT_SCHEMA_PATH,
    codex_auth_config,
    guardian_assessment_permission_config,
    guardian_assessment_permission_profile,
)
from localize.guardian.credentials import CredentialError, SecretCommand
from localize.guardian.executable_trust import (
    ExecutableTrustError,
    require_absolute_trusted_executable,
)
from localize.guardian.filesystem_trust import (
    create_or_wait_for_private_directory,
    is_trusted_directory,
)
from localize.guardian.github import (
    GitHubReader,
    GitHubRepositoryIdentity,
    GitHubRepositoryPolicy,
)
from localize.guardian.models import (
    CodexAuthMode,
    GuardianConfig,
    GuardianMode,
    PipelineConfigSource,
    RepositoryPolicy,
    SigningFormat,
)
from localize.guardian.prevention_runtime import (
    SandboxedTestRunner,
    guardian_prevention_author_permission_config,
    guardian_prevention_author_permission_profile,
)
from localize.guardian.process import (
    ProcessLimits,
    ProcessResourceError,
    WorkspaceQuota,
    linux_cgroup_parent_procs,
    run_bounded_process,
)
from localize.guardian.runtime import (
    GuardianRuntimeError,
    _poll_locking_is_available,
    _preflight_poll_lock,
    _probe_poll_lock_semantics,
    _snapshot_operator_pipeline_configs,
    _validate_private_state_artifacts,
    _validate_subscription_codex_home,
    load_trusted_guardian_config,
)
from localize.guardian.scheduler import (
    LaunchdSchedule,
    SchedulerError,
    render_launchd_plist,
    render_launchd_runner,
)
from localize.guardian.signing import (
    SigningError,
    canonical_signing_key,
    canonical_ssh_fingerprint,
    snapshot_ssh_signing_material,
    ssh_agent_environment,
    ssh_signature_matches,
    signature_matches,
)


_STARTER_CONFIG = """# Generic, report-only Localize Guardian policy.
# Replace every example name and numeric ID with values read from GitHub's API.
# Login names are display labels; numeric IDs grant authority.
mode: observe

# Scheduled invocations catch up once daily after this local wall-clock time.
schedule:
  hour: 0
  minute: 0

runtime:
  codex_model: gpt-5.6-terra
  codex_reasoning_effort: high
  # Uses the operator's Codex/ChatGPT plan allowance, not API billing.
  codex_auth_mode: chatgpt
  # ChatGPT mode only; remove this key when selecting api-key mode.
  codex_home: ~/.local/share/localize-guardian/codex
  # Interactive defaults use PATH. Before `guardian install`, replace these
  # executable names with absolute paths so launchd cannot resolve another binary.
  codex_executable: codex
  git_executable: git
  # OpenPGP remains the backward-compatible default. For agent-backed SSH,
  # replace these signing fields as documented in docs/guardian.md.
  signing_format: openpgp
  signing_program: gpg
  github_token_command: [gh, auth, token]
  # API billing is opt-in: switch codex_auth_mode to api-key, configure this
  # argv-only OS secret-store helper, and enable both USD limits below.
  # codex_api_key_command: [/usr/bin/security, find-generic-password, -w, -s, localize-guardian]
  # Required for write modes; global Git configuration is intentionally ignored.
  # signing_key: REPLACE_WITH_FULL_GPG_FINGERPRINT
  # SSH alternative (all four fields are required together):
  # signing_format: ssh
  # signing_program: /usr/bin/ssh-keygen
  # signing_key: SHA256:REPLACE_WITH_EXACT_PUBLIC_KEY_FINGERPRINT
  # signing_public_key: /absolute/path/to/guardian-signing-key.pub

limits:
  run_timeout_seconds: 1800
  max_attempts: 2
  max_value_edits_per_run: 10
  max_prevention_drafts_per_run: 0
  max_model_calls_per_day: 2
  # API-key mode only:
  # daily_cost_limit_usd: 2.00
  # model_call_reservation_usd: 2.00
  min_apply_confidence: 0.90
  raw_retention_days: 30

repositories:
  - base_repo: acme/widgets
    base_repo_id: 100000001
    base_branch: main
    private_repo_model_opt_in: false
    allowed_pr_authors:
      - login: localization-service[bot]
        id: 100000002
        type: Bot
    allowed_head_owners:
      - login: localization-service[bot]
        id: 100000002
        type: Bot
    allowed_head_repositories:
      - full_name: localization-service/widgets
        id: 100000003
    allowed_branch_globs:
      - "localization/**"
    allowed_path_globs:
      - "src/main/resources/i18n/**"
    # Set to operator to resolve the path beside this Guardian YAML instead of
    # from the exact repository base SHA. See docs/guardian.md for permissions.
    pipeline_config_source: base
    pipeline_config_path: .localize/config.yaml
    source_locale: en
    trusted_reviewers:
      de:
        - login: german-maintainer
          id: 100000004
          type: User
    trusted_bots:
      de:
        - login: translation-reviewer[bot]
          id: 100000005
          type: Bot
"""

_WRITE_MODES = frozenset(
    {
        GuardianMode.APPLY_OWNED_TRANSLATIONS,
        GuardianMode.PROPOSE_PREVENTION,
    }
)
_CODEX_PERMISSION_PROBE = r"""
inside_read=$1
inside_write=$2
outside_read=$3
outside_write=$4
nonce=$5
tcp_port=$6
unix_socket_path=$7
workspace_write=$8
cgroup_parent_procs=$9
unsafe=0

if ! value=$(/bin/cat "$inside_read" 2>/dev/null) || [ "$value" != "$nonce" ]; then
    unsafe=1
fi
if (: > "$inside_write") 2>/dev/null; then
    if [ "$workspace_write" != "1" ]; then
        unsafe=1
    fi
elif [ "$workspace_write" = "1" ]; then
    unsafe=1
fi
if /bin/cat "$outside_read" >/dev/null 2>&1; then
    unsafe=1
fi
if (: > "$outside_write") 2>/dev/null; then
    unsafe=1
fi
if /usr/bin/nc -n -z -w 1 127.0.0.1 "$tcp_port" >/dev/null 2>&1; then
    unsafe=1
fi
if [ -n "$unix_socket_path" ] && \
    /usr/bin/nc -U -z -w 1 "$unix_socket_path" >/dev/null 2>&1; then
    unsafe=1
fi
if [ -n "$cgroup_parent_procs" ] && \
    (: > "$cgroup_parent_procs") 2>/dev/null; then
    unsafe=1
fi

listener_pid=
cleanup() {
    if [ -n "$listener_pid" ]; then
        kill "$listener_pid" >/dev/null 2>&1 || true
        wait "$listener_pid" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT HUP INT TERM
/usr/bin/nc -l 127.0.0.1 0 </dev/null >/dev/null 2>&1 &
listener_pid=$!
/bin/sleep 1
if kill -0 "$listener_pid" >/dev/null 2>&1; then
    unsafe=1
fi
cleanup
listener_pid=
exit "$unsafe"
"""


class GuardianCLIError(RuntimeError):
    """An operator-facing failure whose message contains no secret material."""


@dataclass(frozen=True)
class GuardianInstallPaths:
    """Deterministic, operator-owned files staged by ``guardian install``."""

    label: str
    runner_path: Path
    plist_path: Path
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True)
class _StatusSnapshot:
    last_completed_run: str | None
    pending_revisions: int
    actions: tuple[tuple[str, int], ...]
    health: tuple[tuple[str, str], ...]
    committed_microusd_today: int
    model_calls_today: int


def _resolved_config_path(raw_path: str | Path) -> Path:
    return Path(os.path.abspath(Path(raw_path).expanduser()))


def guardian_state_dir(config_path: str | Path) -> Path:
    """Return the private runtime directory beside an operator config."""

    return _resolved_config_path(config_path).parent / ".guardian"


def guardian_state_path(config_path: str | Path) -> Path:
    """Return the SQLite audit path shared with the Guardian controller."""

    return guardian_state_dir(config_path) / "state.sqlite3"


def _default_launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def guardian_install_paths(config_path: str | Path) -> GuardianInstallPaths:
    """Return deterministic launchd paths without creating them."""

    resolved = _resolved_config_path(config_path)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    label = f"org.localize.guardian.{digest}"
    runtime_dir = guardian_state_dir(resolved)
    return GuardianInstallPaths(
        label=label,
        runner_path=runtime_dir / "launchd-runner.sh",
        plist_path=_default_launch_agents_dir() / f"{label}.plist",
        stdout_path=runtime_dir / "guardian.stdout.log",
        stderr_path=runtime_dir / "guardian.stderr.log",
    )


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or metadata.st_uid == os.getuid()


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise GuardianCLIError(f"Refusing symlinked Guardian directory: {path}")
    try:
        metadata = create_or_wait_for_private_directory(path, parents=True)
    except OSError as exc:
        raise GuardianCLIError(
            f"Could not create private Guardian directory: {path}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise GuardianCLIError(f"Guardian runtime path is not a directory: {path}")
    if not _owned_by_current_user(metadata):
        raise GuardianCLIError(
            f"Guardian runtime directory must be owned by the current user: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise GuardianCLIError(
            f"Guardian runtime directory must have mode 0700: {path}"
        )


def _ensure_operator_directory(path: Path, *, purpose: str = "Scheduling") -> None:
    """Create or validate an operator-owned, non-writable directory."""

    if not path.is_absolute():
        raise GuardianCLIError(f"{purpose} directory must be absolute.")
    root = Path(path.anchor)
    components = tuple(
        root.joinpath(*path.parts[1:index])
        for index in range(1, len(path.parts) + 1)
    )
    for component in components:
        is_leaf = component == path
        if component.is_symlink():
            location = "directory" if is_leaf else "ancestor"
            raise GuardianCLIError(
                f"Refusing symlinked {purpose.casefold()} {location}: {component}"
            )
        if not component.exists():
            try:
                component.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise GuardianCLIError(
                    f"Could not create {purpose.casefold()} directory: {component}"
                ) from exc
        try:
            metadata = component.stat(follow_symlinks=False)
        except OSError as exc:
            raise GuardianCLIError(
                f"Could not inspect {purpose.casefold()} directory ancestor: {component}"
            ) from exc
        trusted_owners = {0}
        if hasattr(os, "getuid"):
            trusted_owners.add(os.getuid())
        if not is_trusted_directory(
            metadata,
            trusted_owners=trusted_owners,
        ):
            location = "directory" if is_leaf else "ancestor"
            raise GuardianCLIError(f"{purpose} {location} is unsafe: {component}")
        if is_leaf and not _owned_by_current_user(metadata):
            raise GuardianCLIError(
                f"{purpose} directory must be owned by the current user: {component}"
            )


def _validate_private_regular_file(path: Path, *, mode: int) -> None:
    if path.is_symlink():
        raise GuardianCLIError(f"Refusing symlinked Guardian file: {path}")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise GuardianCLIError(f"Could not inspect Guardian file: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardianCLIError(f"Guardian path is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise GuardianCLIError(f"Guardian file must not be hard-linked: {path}")
    if not _owned_by_current_user(metadata):
        raise GuardianCLIError(
            f"Guardian file must be owned by the current user: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise GuardianCLIError(f"Guardian file must have mode {mode:04o}: {path}")


def _unlink_created_inode(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            stat.S_ISREG(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == identity
        ):
            path.unlink()
    except OSError:
        pass


def _write_exclusive(path: Path, content: str, *, mode: int) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise GuardianCLIError(f"Refusing to overwrite existing file: {path}") from exc
    except OSError as exc:
        raise GuardianCLIError(f"Could not create Guardian file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = -1
        if "identity" in locals():
            _unlink_created_inode(path, identity)
        if isinstance(exc, OSError):
            raise GuardianCLIError(
                f"Could not complete Guardian file: {path}"
            ) from exc
        raise
    return identity


def _ensure_private_file(path: Path) -> tuple[int, int] | None:
    if path.exists() or path.is_symlink():
        _validate_private_regular_file(path, mode=0o600)
        return None
    return _write_exclusive(path, "", mode=0o600)


def _load_config_or_raise(config_path: Path) -> GuardianConfig:
    try:
        return load_trusted_guardian_config(config_path)
    except GuardianRuntimeError as exc:
        raise GuardianCLIError(str(exc)) from None


def _github_policy(config_policy: RepositoryPolicy) -> GitHubRepositoryPolicy:
    return GitHubRepositoryPolicy(
        repository=config_policy.base_repo,
        repository_id=config_policy.base_repo_id,
        base_branch=config_policy.base_branch,
        allowed_pr_authors=config_policy.allowed_pr_authors,
        allowed_head_owners=config_policy.allowed_head_owners,
        allowed_head_repositories=config_policy.allowed_head_repositories,
        branch_globs=config_policy.allowed_branch_globs,
    )


def _probe_github(config: GuardianConfig) -> tuple[GitHubRepositoryIdentity, ...]:
    """Use the configured argv-only helper for read-only identity probes."""

    identities: list[GitHubRepositoryIdentity] = []
    token = SecretCommand(config.runtime.github_token_command).read()
    try:
        with httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "localize-guardian",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            trust_env=False,
        ) as client:
            for config_policy in config.repositories:
                policy = _github_policy(config_policy)
                identities.append(GitHubReader(client, policy).repository_identity())
    finally:
        token = ""
    return tuple(identities)


def _signing_key_configured(
    configured_key: str | None,
    *,
    git_executable: str,
    signing_program: str,
    signing_format: SigningFormat = SigningFormat.OPENPGP,
    signing_public_key: str | None = None,
    temporary_root: Path | None = None,
) -> bool:
    """Prove the exact configured key can sign in Guardian's isolated context."""

    if signing_format is SigningFormat.SSH:
        return _ssh_signing_key_configured(
            configured_key,
            signing_public_key=signing_public_key,
            git_executable=git_executable,
            signing_program=signing_program,
            temporary_root=temporary_root,
        )
    if signing_format is not SigningFormat.OPENPGP or signing_public_key is not None:
        return False

    try:
        configured_key = canonical_signing_key(configured_key)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if not _command_available((git_executable,)) or not _command_available(
        (signing_program,)
    ):
        return False
    configured_home = os.environ.get("GNUPGHOME")
    signing_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".gnupg"
    )
    if not signing_home.is_dir() or signing_home.is_symlink():
        return False
    try:
        resolved_signing_home = signing_home.resolve(strict=True)
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="localize-guardian-signing-probe-") as raw:
            root = Path(raw)
            isolated_home = root / "home"
            repository = root / "repository"
            isolated_home.mkdir(mode=0o700)
            environment = {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GNUPGHOME": str(resolved_signing_home),
                "HOME": str(isolated_home),
                "LC_ALL": "C",
                "PATH": os.defpath,
            }
            git_prefix = [
                git_executable,
                "-c",
                f"gpg.program={signing_program}",
            ]
            commands = (
                [*git_prefix, "init", "--quiet", str(repository)],
                [
                    *git_prefix,
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Localize Guardian",
                    "-c",
                    "user.email=localize-guardian@users.noreply.github.com",
                    "commit",
                    "--allow-empty",
                    "--no-verify",
                    f"-S{configured_key}",
                    "--message=Localize Guardian signing probe",
                ],
                [
                    *git_prefix,
                    "-C",
                    str(repository),
                    "verify-commit",
                    "--raw",
                    "HEAD",
                ],
            )
            process_limits = ProcessLimits.for_timeout(
                20,
                max_file_size_bytes=8 * 1024 * 1024,
            )
            workspace_quota = WorkspaceQuota.capture(
                root,
                max_growth_bytes=16 * 1024 * 1024,
                max_added_entries=1_000,
            )
            verification_output = ""
            for command in commands:
                completed = run_bounded_process(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=20,
                    env=environment,
                    start_new_session=True,
                    limits=process_limits,
                    workspace_quota=workspace_quota,
                )
                if completed.returncode != 0:
                    return False
                verification_output = "\n".join(
                    (completed.stdout or "", completed.stderr or "")
                )
            if not signature_matches(verification_output, configured_key):
                return False
    except (OSError, subprocess.SubprocessError, ProcessResourceError):
        return False
    return True


def _ssh_signing_key_configured(
    configured_key: str | None,
    *,
    signing_public_key: str | None,
    git_executable: str,
    signing_program: str,
    temporary_root: Path | None,
) -> bool:
    """Actually sign and verify with one exact agent-backed SSH public key."""

    try:
        fingerprint = canonical_ssh_fingerprint(configured_key)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if signing_public_key is None:
        return False
    if not _command_available((git_executable,)) or not _command_available(
        (signing_program,)
    ):
        return False
    try:
        require_absolute_trusted_executable(
            (signing_program,),
            field="runtime.signing_program",
        )
        parent = None if temporary_root is None else str(temporary_root)
        with tempfile.TemporaryDirectory(
            prefix="localize-guardian-ssh-probe-",
            dir=parent,
        ) as raw:
            root = Path(raw).resolve(strict=True)
            root.chmod(0o700)
            isolated_home = root / "home"
            repository = root / "repository"
            isolated_home.mkdir(mode=0o700)
            with snapshot_ssh_signing_material(
                public_key_path=signing_public_key,
                expected_fingerprint=fingerprint,
                signing_program=signing_program,
                temporary_root=root,
            ) as material:
                signing_socket = ssh_agent_environment(
                    temporary_root=material.root,
                )
                environment = {
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    "HOME": str(isolated_home),
                    "LC_ALL": "C",
                    "PATH": os.defpath,
                }
                git_prefix = [
                    git_executable,
                    "-c",
                    "gpg.format=ssh",
                    "-c",
                    f"gpg.ssh.program={signing_program}",
                    "-c",
                    f"gpg.ssh.allowedSignersFile={material.allowed_signers}",
                    "-c",
                    "gpg.minTrustLevel=fully",
                ]
                commands = (
                    (
                        [*git_prefix, "init", "--quiet", str(repository)],
                        environment,
                    ),
                    (
                        [
                            *git_prefix,
                            "-C",
                            str(repository),
                            "-c",
                            "user.name=Localize Guardian",
                            "-c",
                            "user.email=localize-guardian@users.noreply.github.com",
                            "commit",
                            "--allow-empty",
                            "--no-verify",
                            f"-S{material.public_key}",
                            "--message=Localize Guardian signing probe",
                        ],
                        {**environment, **signing_socket},
                    ),
                    (
                        [
                            *git_prefix,
                            "-C",
                            str(repository),
                            "verify-commit",
                            "--raw",
                            "HEAD",
                        ],
                        environment,
                    ),
                )
                process_limits = ProcessLimits.for_timeout(
                    20,
                    max_file_size_bytes=8 * 1024 * 1024,
                )
                workspace_quota = WorkspaceQuota.capture(
                    root,
                    max_growth_bytes=16 * 1024 * 1024,
                    max_added_entries=1_000,
                )
                verification_output = ""
                for command, command_environment in commands:
                    completed = run_bounded_process(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        shell=False,
                        timeout=20,
                        env=command_environment,
                        start_new_session=True,
                        limits=process_limits,
                        workspace_quota=workspace_quota,
                    )
                    if completed.returncode != 0:
                        return False
                    verification_output = "\n".join(
                        (completed.stdout or "", completed.stderr or "")
                    )
                return ssh_signature_matches(verification_output, fingerprint)
    except (
        ExecutableTrustError,
        OSError,
        SigningError,
        ValueError,
        subprocess.SubprocessError,
        ProcessResourceError,
    ):
        return False


def _command_available(command: Sequence[str]) -> bool:
    if not command:
        return False
    executable = command[0]
    if "/" in executable:
        path = Path(executable).expanduser()
        return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def _require_absolute_executable(command: Sequence[str], *, field: str) -> None:
    try:
        require_absolute_trusted_executable(command, field=field)
    except ExecutableTrustError as exc:
        raise GuardianCLIError(str(exc)) from None


def _credential_helper_works(command: Sequence[str]) -> bool:
    """Validate one credential helper while discarding its secret output."""

    if not _command_available(command):
        return False
    try:
        SecretCommand(tuple(command)).read()
    except (CredentialError, ValueError):
        return False
    return True


def _codex_home(config: GuardianConfig) -> Path:
    return Path(os.path.abspath(Path(config.runtime.codex_home).expanduser()))


def _codex_login_environment(codex_home: Path) -> dict[str, str]:
    environment = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(Path.home()),
        "NO_COLOR": "1",
        "PATH": os.defpath,
    }
    for key in (
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
    ):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _codex_chatgpt_login_ready(config: GuardianConfig) -> bool:
    """Verify the dedicated file-backed login without making a model call."""

    if config.runtime.codex_auth_mode is not CodexAuthMode.CHATGPT:
        return False
    try:
        codex_home = _validate_subscription_codex_home(config)
        settings = [
            argument
            for setting in codex_auth_config(CodexAuthMode.CHATGPT)
            for argument in ("-c", setting)
        ]
        completed = run_bounded_process(
            [
                config.runtime.codex_executable,
                "login",
                *settings,
                "status",
            ],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=_codex_login_environment(codex_home),
            start_new_session=True,
            limits=ProcessLimits.for_timeout(
                30,
                max_file_size_bytes=2 * 1024 * 1024,
            ),
        )
        if completed.returncode != 0:
            return False
        status = "\n".join((completed.stdout or "", completed.stderr or ""))
        if "chatgpt" not in status.casefold():
            return False
        _validate_subscription_codex_home(config)
    except (OSError, subprocess.SubprocessError, ProcessResourceError, GuardianRuntimeError):
        return False
    return True


def _cmd_login(args: argparse.Namespace) -> int:
    """Create or refresh the dedicated Guardian ChatGPT subscription login."""

    config_path = _resolved_config_path(args.config)
    try:
        config = _load_config_or_raise(config_path)
        if config.runtime.codex_auth_mode is not CodexAuthMode.CHATGPT:
            raise GuardianCLIError(
                "guardian login is available only with codex_auth_mode: chatgpt."
            )
        if not _command_available((config.runtime.codex_executable,)):
            raise GuardianCLIError("Codex executable was not found.")
        codex_home = _codex_home(config)
        _ensure_operator_directory(codex_home, purpose="Codex home")
        _ensure_private_directory(codex_home)
        auth_file = codex_home / "auth.json"
        if auth_file.exists() or auth_file.is_symlink():
            _validate_private_regular_file(auth_file, mode=0o600)
        settings = [
            argument
            for setting in codex_auth_config(CodexAuthMode.CHATGPT)
            for argument in ("-c", setting)
        ]
        previous_umask = os.umask(0o077)
        try:
            completed = subprocess.run(
                [
                    config.runtime.codex_executable,
                    "login",
                    *settings,
                    "--device-auth",
                ],
                shell=False,
                check=False,
                env=_codex_login_environment(codex_home),
            )
        finally:
            os.umask(previous_umask)
        if completed.returncode != 0 or not _codex_chatgpt_login_ready(config):
            raise GuardianCLIError("Codex ChatGPT login did not complete safely.")
    except GuardianCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"error: Guardian login failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1
    print(f"Codex ChatGPT subscription login ready: {codex_home}")
    return 0


def _check_schema() -> None:
    try:
        schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise GuardianCLIError("Guardian result schema is missing or invalid.") from exc


@contextmanager
def _doctor_network_canaries() -> Iterator[tuple[int, str]]:
    """Hold live TCP and Unix endpoints that an effective profile cannot reach."""

    unix_root: Path | None = None
    unix_listener: socket.socket | None = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_listener:
        tcp_listener.bind(("127.0.0.1", 0))
        tcp_listener.listen(1)
        unix_socket_path = ""
        try:
            if hasattr(socket, "AF_UNIX"):
                unix_root = Path(tempfile.mkdtemp(prefix="lg-"))
                unix_socket_path = str(unix_root / "canary.sock")
                unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                unix_listener.bind(unix_socket_path)
                unix_listener.listen(1)
            yield int(tcp_listener.getsockname()[1]), unix_socket_path
        finally:
            if unix_listener is not None:
                unix_listener.close()
            if unix_root is not None:
                shutil.rmtree(unix_root)


def _codex_capability_probe(
    executable: str,
    *,
    auth_mode: CodexAuthMode = CodexAuthMode.CHATGPT,
    authoring: bool = False,
) -> bool:
    """Prove exact non-interactive flags and one named profile without a model call."""

    try:
        with tempfile.TemporaryDirectory(prefix="localize-guardian-doctor-") as raw:
            root = Path(raw)
            root.chmod(0o700)
            home = root / "home"
            codex_home = root / "codex-home"
            evidence = root / "evidence"
            for directory in (home, codex_home, evidence):
                directory.mkdir(mode=0o700)
            nonce = os.urandom(16).hex()
            inside_read = evidence / "inside-read"
            outside_read = root / "outside-read"
            inside_read.write_text(nonce, encoding="utf-8")
            outside_read.write_text(nonce, encoding="utf-8")
            inside_read.chmod(0o400)
            outside_read.chmod(0o400)
            environment = {
                "CODEX_HOME": str(codex_home),
                "HOME": str(home),
                "NO_COLOR": "1",
                "PATH": os.environ.get("PATH", os.defpath),
                "TMPDIR": str(root),
            }
            permission_config = (
                guardian_prevention_author_permission_config()
                if authoring
                else guardian_assessment_permission_config()
            )
            permission_profile = (
                guardian_prevention_author_permission_profile()
                if authoring
                else guardian_assessment_permission_profile()
            )
            config_arguments = [
                argument
                for setting in (*codex_auth_config(auth_mode), *permission_config)
                for argument in ("-c", setting)
            ]
            process_limits = ProcessLimits.for_timeout(
                15,
                max_file_size_bytes=4 * 1024 * 1024,
                require_linux_cgroup=True,
            )
            cgroup_escape_target = linux_cgroup_parent_procs()
            workspace_quota = WorkspaceQuota.capture(
                root,
                max_growth_bytes=16 * 1024 * 1024,
                max_added_entries=1_000,
            )
            flag_probe = run_bounded_process(
                [
                    executable,
                    "--ask-for-approval",
                    "never",
                    *config_arguments,
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--strict-config",
                    "--help",
                ],
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=environment,
                start_new_session=True,
                limits=process_limits,
                workspace_quota=workspace_quota,
            )
            if flag_probe.returncode != 0:
                return False

            with _doctor_network_canaries() as (tcp_port, unix_socket_path):
                permission_probe = run_bounded_process(
                    [
                        executable,
                        *config_arguments,
                        "sandbox",
                        "--permission-profile",
                        permission_profile,
                        "--cd",
                        str(evidence),
                        "--",
                        "/bin/sh",
                        "-c",
                        _CODEX_PERMISSION_PROBE,
                        "guardian-permission-probe",
                        str(inside_read),
                        str(evidence / "inside-write"),
                        str(outside_read),
                        str(root / "outside-write"),
                        nonce,
                        str(tcp_port),
                        unix_socket_path,
                        "1" if authoring else "0",
                        (
                            str(cgroup_escape_target)
                            if cgroup_escape_target is not None
                            else ""
                        ),
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                    env=environment,
                    start_new_session=True,
                    limits=process_limits,
                    workspace_quota=workspace_quota,
                )
            return permission_probe.returncode == 0
    except (OSError, subprocess.SubprocessError, ProcessResourceError):
        return False


def _prevention_sandbox_probe(config: GuardianConfig) -> bool:
    """Validate configured test executables and prove each sandbox prefix."""

    seen_prefixes: set[tuple[str, ...]] = set()
    try:
        for repository in config.repositories:
            policy = repository.prevention
            if policy is None:
                continue
            _require_absolute_executable(
                policy.sandbox_argv_prefix,
                field=f"{repository.base_repo} prevention sandbox",
            )
            for index, argv in enumerate(policy.focused_test_argv):
                _require_absolute_executable(
                    argv,
                    field=f"{repository.base_repo} focused test {index}",
                )
            if policy.sandbox_argv_prefix in seen_prefixes:
                continue
            seen_prefixes.add(policy.sandbox_argv_prefix)
            with tempfile.TemporaryDirectory(
                prefix="localize-guardian-sandbox-doctor-"
            ) as raw:
                root = Path(raw)
                workspace = root / "workspace"
                private = root / "private"
                workspace.mkdir(mode=0o700)
                private.mkdir(mode=0o700)
                runner = SandboxedTestRunner(
                    timeout_seconds=min(15.0, config.limits.run_timeout_seconds)
                )
                runner._prove_confinement(
                    workspace=workspace,
                    private=private,
                    sandbox_prefix=policy.sandbox_argv_prefix,
                    environment=runner._environment(home=private, temp=private),
                )
    except (GuardianCLIError, OSError, ValueError):
        return False
    return bool(seen_prefixes)


def _cmd_init(args: argparse.Namespace) -> int:
    config_path = _resolved_config_path(args.config)
    if config_path.exists() or config_path.is_symlink():
        print(f"error: configuration already exists: {config_path}", file=sys.stderr)
        return 1
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_directory(guardian_state_dir(config_path))
        _write_exclusive(config_path, _STARTER_CONFIG, mode=0o600)
    except GuardianCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created report-only Guardian config: {config_path}")
    print("Replace every example identity before the first run.")
    return 0


def _state_directory_doctor(config_path: Path) -> tuple[str, bool]:
    runtime_dir = guardian_state_dir(config_path)
    if not _poll_locking_is_available():
        return "error (process locking is unavailable)", False
    if not runtime_dir.exists() and not runtime_dir.is_symlink():
        if os.access(runtime_dir.parent, os.W_OK):
            try:
                _probe_poll_lock_semantics(runtime_dir.parent)
            except GuardianRuntimeError:
                return "error (paths must be regular and private)", False
            return "ready (created on first run or install)", True
        return "error (parent directory is not writable)", False
    try:
        _ensure_private_directory(runtime_dir)
        state_path = guardian_state_path(config_path)
        _validate_private_state_artifacts(state_path)
        _preflight_poll_lock(runtime_dir)
        _probe_poll_lock_semantics(runtime_dir)
    except (GuardianCLIError, GuardianRuntimeError):
        return "error (paths must be regular and private)", False
    return "ok", True


def _existing_safe_probe_parent(path: Path) -> Path | None:
    """Return an existing trusted parent without creating or changing it."""

    trusted_owners = {0}
    if hasattr(os, "getuid"):
        trusted_owners.add(os.getuid())
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or not _owned_by_current_user(metadata)
            or not is_trusted_directory(
                metadata,
                trusted_owners=trusted_owners,
            )
        ):
            return None
        for ancestor in path.parents:
            ancestor_metadata = ancestor.lstat()
            if stat.S_ISLNK(ancestor_metadata.st_mode) or not is_trusted_directory(
                ancestor_metadata,
                trusted_owners=trusted_owners,
            ):
                return None
    except OSError:
        return None
    return path


def _operator_pipeline_config_doctor(
    config: GuardianConfig,
    *,
    config_path: Path,
) -> tuple[str, bool]:
    """Exercise the production snapshot path without external or model work."""

    operator_count = sum(
        repository.pipeline_config_source is PipelineConfigSource.OPERATOR
        for repository in config.repositories
    )
    if operator_count == 0:
        return "not configured (repository base mode)", True

    runtime_dir = guardian_state_dir(config_path)
    try:
        if runtime_dir.exists():
            _ensure_private_directory(runtime_dir)
            with _snapshot_operator_pipeline_configs(
                config=config,
                guardian_config_path=config_path,
                state_directory=runtime_dir,
            ) as snapshots:
                if len(snapshots) != operator_count:
                    raise GuardianRuntimeError(
                        "Guardian operator pipeline config is unavailable or unsafe."
                    )
        else:
            with tempfile.TemporaryDirectory(
                prefix=".guardian-operator-config-doctor-",
                dir=config_path.parent,
            ) as temporary_directory:
                scratch = Path(temporary_directory)
                scratch.chmod(0o700)
                with _snapshot_operator_pipeline_configs(
                    config=config,
                    guardian_config_path=config_path,
                    state_directory=scratch,
                ) as snapshots:
                    if len(snapshots) != operator_count:
                        raise GuardianRuntimeError(
                            "Guardian operator pipeline config is unavailable or unsafe."
                        )
    except (GuardianCLIError, GuardianRuntimeError, OSError):
        return "error (private config or glossary is unavailable or unsafe)", False
    return f"ok ({operator_count} snapshotted)", True


def _cmd_doctor(args: argparse.Namespace) -> int:
    config_path = _resolved_config_path(args.config)
    print("Guardian doctor")
    try:
        config = _load_config_or_raise(config_path)
    except GuardianCLIError as exc:
        print(f"config: error ({exc})")
        return 1

    healthy = True
    print(f"config: ok ({config.mode.value})")
    state_status, state_ok = _state_directory_doctor(config_path)
    print(f"state directory: {state_status}")
    healthy &= state_ok

    operator_status, operator_ok = _operator_pipeline_config_doctor(
        config,
        config_path=config_path,
    )
    print(f"operator pipeline configs: {operator_status}")
    healthy &= operator_ok
    if not operator_ok:
        return 1

    codex_path = _command_available((config.runtime.codex_executable,))
    print(f"Codex executable: {'ok' if codex_path else 'error (not found)'}")
    print(
        f"Codex model: {config.runtime.codex_model} "
        f"({config.runtime.codex_reasoning_effort})"
    )
    healthy &= codex_path
    codex_capabilities = (
        _codex_capability_probe(
            config.runtime.codex_executable,
            auth_mode=config.runtime.codex_auth_mode,
        )
        if codex_path
        else False
    )
    print(
        "Codex capability canary: "
        f"{'ok' if codex_capabilities else 'error (flags or confinement unavailable)'}"
    )
    healthy &= codex_capabilities
    if config.mode is GuardianMode.PROPOSE_PREVENTION:
        author_capabilities = (
            _codex_capability_probe(
                config.runtime.codex_executable,
                auth_mode=config.runtime.codex_auth_mode,
                authoring=True,
            )
            if codex_path
            else False
        )
        print(
            "Codex authoring canary: "
            f"{'ok' if author_capabilities else 'error (confinement unavailable)'}"
        )
        healthy &= author_capabilities
        prevention_sandbox = _prevention_sandbox_probe(config)
        print(
            "Prevention test sandbox canary: "
            f"{'ok' if prevention_sandbox else 'error (confinement unavailable)'}"
        )
        healthy &= prevention_sandbox
    try:
        _check_schema()
    except GuardianCLIError:
        print("result schema: error")
        healthy = False
    else:
        print("result schema: ok")

    git_ready = _command_available((config.runtime.git_executable,))
    write_mode = config.mode in _WRITE_MODES
    signing_program_ready = (
        _command_available((config.runtime.signing_program,)) if write_mode else True
    )
    github_helper = _command_available(config.runtime.github_token_command)
    print(f"Git executable: {'ok' if git_ready else 'error (not found)'}")
    if write_mode:
        print(
            "Signing program: "
            f"{'ok' if signing_program_ready else 'error (not found)'}"
        )
    else:
        print(f"Signing program: not required ({config.mode.value} mode)")
    print(
        "GitHub credential helper: "
        f"{'ok' if github_helper else 'error (executable not found)'}"
    )
    healthy &= git_ready and github_helper
    if write_mode:
        healthy &= signing_program_ready

    if github_helper:
        try:
            identities = _probe_github(config)
        except Exception:
            print("GitHub read-only probe: error")
            healthy = False
        else:
            for identity in identities:
                visibility = "private" if identity.private else "public"
                print(
                    f"repository {identity.full_name}: ok "
                    f"({visibility}, id={identity.repository_id})"
                )

    api_key_command = config.runtime.codex_api_key_command
    if config.runtime.codex_auth_mode is CodexAuthMode.CHATGPT:
        chatgpt_ready = codex_path and _codex_chatgpt_login_ready(config)
        print(
            "Codex ChatGPT subscription login: "
            f"{'ok' if chatgpt_ready else 'error (run guardian login)'}"
        )
        healthy &= chatgpt_ready
    elif api_key_command:
        api_key_ready = _credential_helper_works(api_key_command)
        print(f"Codex API key helper: {'ok' if api_key_ready else 'error'}")
        healthy &= api_key_ready
    else:
        print("Codex API credential: error (configure a helper)")
        healthy = False

    configured_signing_key = config.runtime.signing_key
    state_directory = guardian_state_dir(config_path)
    signing_probe_root = (
        state_directory
        if state_ok and state_directory.is_dir() and not state_directory.is_symlink()
        else _existing_safe_probe_parent(config_path.parent)
    )
    if not write_mode:
        print(f"commit signing: not required ({config.mode.value} mode)")
    elif git_ready and signing_program_ready and _signing_key_configured(
        configured_signing_key,
        git_executable=config.runtime.git_executable,
        signing_program=config.runtime.signing_program,
        signing_format=config.runtime.signing_format,
        signing_public_key=config.runtime.signing_public_key,
        temporary_root=signing_probe_root,
    ):
        print(
            "commit signing: exact key signed and verified in isolation "
            "(verified again before publication)"
        )
    else:
        if configured_signing_key is None:
            print(
                "commit signing: error "
                "(write modes require explicit runtime.signing_key)"
            )
        else:
            print(
                "commit signing: error "
                "(configured key could not sign and verify in isolation)"
            )
        healthy = False

    adapters = list_localization_adapters()
    print(
        "localization adapters: "
        f"ok ({len(adapters)} registered; project adapter checked after base fetch)"
    )
    return 0 if healthy else 1


def _cmd_run(args: argparse.Namespace) -> int:
    config_path = _resolved_config_path(args.config)
    try:
        _load_config_or_raise(config_path)
        _ensure_private_directory(guardian_state_dir(config_path))
        controller = importlib.import_module("localize.guardian.controller")
        run_once = getattr(controller, "run_once")
        result = run_once(config_path=config_path, scheduled=bool(args.scheduled))
        if result is None:
            return 0
        if isinstance(result, bool) or not isinstance(result, int):
            raise TypeError(
                "Guardian controller returned an unsupported result type."
            )
        return result
    except GuardianRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"error: Guardian run failed ({type(exc).__name__}); inspect the private audit log.",
            file=sys.stderr,
        )
        return 1


def _status_snapshot(state_path: Path, *, mode: GuardianMode) -> _StatusSnapshot:
    resolution_modes = {
        GuardianMode.OBSERVE: tuple(item.value for item in GuardianMode),
        GuardianMode.PREPARE: (
            GuardianMode.PREPARE.value,
            GuardianMode.APPLY_OWNED_TRANSLATIONS.value,
            GuardianMode.PROPOSE_PREVENTION.value,
        ),
        GuardianMode.APPLY_OWNED_TRANSLATIONS: (
            GuardianMode.APPLY_OWNED_TRANSLATIONS.value,
            GuardianMode.PROPOSE_PREVENTION.value,
        ),
        GuardianMode.PROPOSE_PREVENTION: (GuardianMode.PROPOSE_PREVENTION.value,),
    }[mode]
    placeholders = ", ".join("?" for _ in resolution_modes)
    uri = f"{state_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        last_run_row = connection.execute(
            """
            SELECT finished_at FROM runs
            WHERE status = 'completed'
            ORDER BY finished_at DESC, run_id DESC
            LIMIT 1
            """
        ).fetchone()
        pending_row = connection.execute(
            f"""
            SELECT COUNT(*) FROM event_revisions AS e
            WHERE NOT EXISTS (
                SELECT 1 FROM actions AS a
                JOIN runs AS r ON r.run_id = a.run_id
                WHERE a.event_revision_id = e.revision_id
                  AND a.status IN ('completed', 'skipped')
                  AND r.mode IN ({placeholders})
            )
            """,
            resolution_modes,
        ).fetchone()
        actions = tuple(
            (str(row[0]), int(row[1]))
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM actions GROUP BY status ORDER BY status"
            ).fetchall()
        )
        health = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT current.component, current.status
                FROM health AS current
                JOIN (
                    SELECT component, MAX(health_id) AS health_id
                    FROM health GROUP BY component
                ) AS latest ON latest.health_id = current.health_id
                ORDER BY current.component
                """
            ).fetchall()
        )
        today_date = datetime.now(timezone.utc).date()
        today = today_date.isoformat()
        tomorrow_iso = (today_date + timedelta(days=1)).isoformat()
        committed_row = connection.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(amount_microusd), 0) FROM costs
                 WHERE incurred_at >= ? AND incurred_at < ?)
                +
                (SELECT COALESCE(SUM(amount_microusd), 0)
                 FROM budget_reservations
                 WHERE reserved_at >= ? AND reserved_at < ?
                   AND status IN ('reserved', 'unknown'))
            """,
            (today, tomorrow_iso, today, tomorrow_iso),
        ).fetchone()
        model_calls_row = connection.execute(
            """
            SELECT COUNT(*) FROM model_call_reservations
            WHERE reserved_at >= ? AND reserved_at < ?
              AND status IN ('reserved', 'completed', 'unknown')
            """,
            (today, tomorrow_iso),
        ).fetchone()
    return _StatusSnapshot(
        last_completed_run=str(last_run_row[0]) if last_run_row else None,
        pending_revisions=int(pending_row[0]),
        actions=actions,
        health=health,
        committed_microusd_today=int(committed_row[0]),
        model_calls_today=int(model_calls_row[0]),
    )


def _cmd_status(args: argparse.Namespace) -> int:
    config_path = _resolved_config_path(args.config)
    try:
        config = _load_config_or_raise(config_path)
    except GuardianCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Guardian status")
    print(f"mode: {config.mode.value}")
    state_path = guardian_state_path(config_path)
    if not state_path.exists():
        print("state: no runs recorded")
        return 0
    try:
        _validate_private_regular_file(state_path, mode=0o600)
        if state_path.stat().st_size == 0:
            print("state: no runs recorded")
            return 0
        snapshot = _status_snapshot(state_path, mode=config.mode)
    except (GuardianCLIError, OSError, sqlite3.Error):
        print("error: Guardian state is unavailable or invalid.", file=sys.stderr)
        return 1

    print(f"last completed run: {snapshot.last_completed_run or 'none'}")
    print(f"pending feedback revisions: {snapshot.pending_revisions}")
    actions = ", ".join(f"{status}={count}" for status, count in snapshot.actions)
    print(f"actions: {actions or 'none'}")
    health = ", ".join(f"{component}={status}" for component, status in snapshot.health)
    print(f"health: {health or 'none'}")
    print(
        "UTC daily model calls: "
        f"{snapshot.model_calls_today}/{config.limits.max_model_calls_per_day}"
    )
    if config.runtime.codex_auth_mode is CodexAuthMode.API_KEY:
        print(
            "UTC daily committed API cost: "
            f"${snapshot.committed_microusd_today / 1_000_000:.6f}"
        )
    return 0


def _resolve_localize_executable(raw_executable: str | None) -> Path:
    if raw_executable:
        executable = Path(raw_executable).expanduser().resolve()
    else:
        discovered = shutil.which("localize")
        if discovered is None:
            raise GuardianCLIError(
                "Could not find the localize executable; pass --executable with an absolute path."
            )
        executable = Path(discovered).resolve()
    _require_absolute_executable((str(executable),), field="Localize executable")
    return executable


def _cmd_install(args: argparse.Namespace) -> int:
    config_path = _resolved_config_path(args.config)
    created_files: list[tuple[Path, int, int]] = []

    def remember_created(path: Path, identity: tuple[int, int]) -> None:
        created_files.append((path, *identity))

    def rollback_created() -> None:
        for path, expected_device, expected_inode in reversed(created_files):
            try:
                metadata = path.stat(follow_symlinks=False)
                if (
                    metadata.st_dev == expected_device
                    and metadata.st_ino == expected_inode
                    and stat.S_ISREG(metadata.st_mode)
                ):
                    path.unlink()
            except OSError:
                continue

    try:
        config = _load_config_or_raise(config_path)
        if config.runtime.codex_auth_mode is CodexAuthMode.CHATGPT:
            if not _codex_chatgpt_login_ready(config):
                raise GuardianCLIError(
                    "Scheduled runs require a dedicated ChatGPT login; run guardian login."
                )
        elif not config.runtime.codex_api_key_command:
            raise GuardianCLIError(
                "API-key scheduled runs require runtime.codex_api_key_command."
            )
        _require_absolute_executable(
            (config.runtime.codex_executable,),
            field="runtime.codex_executable",
        )
        _require_absolute_executable(
            (config.runtime.git_executable,),
            field="runtime.git_executable",
        )
        if config.mode in _WRITE_MODES:
            _require_absolute_executable(
                (config.runtime.signing_program,),
                field="runtime.signing_program",
            )
        _require_absolute_executable(
            config.runtime.github_token_command,
            field="runtime.github_token_command",
        )
        if config.runtime.codex_auth_mode is CodexAuthMode.API_KEY:
            _require_absolute_executable(
                config.runtime.codex_api_key_command,
                field="runtime.codex_api_key_command",
            )
        executable = _resolve_localize_executable(args.executable)
        paths = guardian_install_paths(config_path)
        if paths.runner_path.exists() or paths.runner_path.is_symlink():
            raise GuardianCLIError(
                f"Scheduler runner already exists: {paths.runner_path}"
            )
        if paths.plist_path.exists() or paths.plist_path.is_symlink():
            raise GuardianCLIError(f"LaunchAgent already exists: {paths.plist_path}")
        _ensure_private_directory(guardian_state_dir(config_path))
        _ensure_operator_directory(paths.plist_path.parent)
        for log_path in (paths.stdout_path, paths.stderr_path):
            identity = _ensure_private_file(log_path)
            if identity is not None:
                remember_created(log_path, identity)
        runner = render_launchd_runner(executable=executable, config_path=config_path)
        plist = render_launchd_plist(
            LaunchdSchedule(
                label=paths.label,
                runner_path=paths.runner_path,
                stdout_path=paths.stdout_path,
                stderr_path=paths.stderr_path,
            )
        )
        runner_identity = _write_exclusive(paths.runner_path, runner, mode=0o700)
        remember_created(paths.runner_path, runner_identity)
        plist_identity = _write_exclusive(paths.plist_path, plist, mode=0o600)
        remember_created(paths.plist_path, plist_identity)
    except (GuardianCLIError, SchedulerError) as exc:
        rollback_created()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        rollback_created()
        print(
            f"error: Guardian installation failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1

    print(f"LaunchAgent staged but not loaded: {paths.plist_path}")
    print(f"runner: {paths.runner_path}")
    print(f"stdout: {paths.stdout_path}")
    print(f"stderr: {paths.stderr_path}")
    print("Inspect these files, then load the LaunchAgent explicitly when ready.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localize guardian",
        description="Operate the optional, self-hosted localization PR Guardian.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create a report-only starter policy."
    )
    init_parser.add_argument(
        "--config", required=True, help="Operator-owned Guardian YAML path."
    )
    init_parser.set_defaults(func=_cmd_init)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Run read-only prerequisite checks."
    )
    doctor_parser.add_argument(
        "--config", required=True, help="Operator-owned Guardian YAML path."
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

    login_parser = subparsers.add_parser(
        "login",
        help="Authenticate the dedicated Guardian Codex home with ChatGPT.",
    )
    login_parser.add_argument(
        "--config", required=True, help="Operator-owned Guardian YAML path."
    )
    login_parser.set_defaults(func=_cmd_login)

    run_parser = subparsers.add_parser("run", help="Perform one bounded Guardian poll.")
    run_parser.add_argument(
        "--config", required=True, help="Operator-owned Guardian YAML path."
    )
    run_parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    run_parser.set_defaults(func=_cmd_run)

    status_parser = subparsers.add_parser(
        "status", help="Print a redacted audit summary."
    )
    status_parser.add_argument(
        "--config", required=True, help="Operator-owned Guardian YAML path."
    )
    status_parser.set_defaults(func=_cmd_status)

    install_parser = subparsers.add_parser(
        "install",
        help="Stage a secret-free macOS LaunchAgent for operator inspection.",
    )
    install_parser.add_argument(
        "--config", required=True, help="Operator-owned Guardian YAML path."
    )
    install_parser.add_argument(
        "--executable",
        default=None,
        help="Absolute localize executable path (defaults to PATH lookup).",
    )
    install_parser.set_defaults(func=_cmd_install)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
