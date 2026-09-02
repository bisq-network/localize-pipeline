"""Production-only assembly for one bounded Localize Guardian poll.

The core controller is dependency-injected and fully testable without network
or credential access.  This module is the deliberately small trust boundary
that connects it to GitHub, Codex, ephemeral Git workspaces, and private local
state.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import time
from typing import Any

import httpx
import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - Guardian scheduling is POSIX-only today.
    fcntl = None  # type: ignore[assignment]

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
from localize.guardian.filesystem_trust import (
    create_or_wait_for_private_directory,
    is_trusted_directory,
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
    GuardianSchedule,
    PipelineConfigSnapshot,
    PipelineConfigSource,
    PreventionPolicy,
    RepositoryPolicy,
    SigningFormat,
)
from localize.guardian.prevention_runtime import (
    PreventionCodexAuthor,
    PreventionCoordinator,
    PreventionGitHubBroker,
    SandboxedTestRunner,
)
from localize.guardian.scheduler import is_run_due
from localize.guardian.signing import (
    SSHSigningMaterial,
    SigningError,
    canonical_signing_key,
    canonical_ssh_fingerprint,
    snapshot_ssh_signing_material,
)
from localize.guardian.state import GuardianState, _validate_sqlite_state_artifacts
from localize.guardian.workspace import ExactRevision, materialize_exact_checkout


_GITHUB_API_URL = "https://api.github.com"
_GITHUB_HOST = "github.com"
_POLL_ATTEMPT_COMPONENT = "guardian-poll-attempt"
_MAX_CONFIG_BYTES = 1_048_576
_MAX_CODEX_AUTH_BYTES = 1_048_576
_MAX_OPERATOR_PIPELINE_FILE_BYTES = 1_048_576
_POLL_LOCK_INITIALIZATION_TIMEOUT_SECONDS = 1.0
_POLL_LOCK_RETRY_SECONDS = 0.002
_WRITE_MODES = frozenset(
    {
        GuardianMode.APPLY_OWNED_TRANSLATIONS,
        GuardianMode.PROPOSE_PREVENTION,
    }
)


class GuardianRuntimeError(RuntimeError):
    """A redacted production-wiring failure safe for operator output."""


class _GuardianPollAlreadyRunning(RuntimeError):
    """Signal that another process owns this config's exclusive poll lock."""


def _resolved_config_path(path: str | Path) -> Path:
    """Return an absolute path without silently following a symlinked leaf."""

    return Path(os.path.abspath(Path(path).expanduser()))


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
        if ancestor.is_symlink() or not is_trusted_directory(
            metadata,
            trusted_owners=owners,
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
        metadata = create_or_wait_for_private_directory(directory)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise GuardianRuntimeError("Guardian state path must remain private.")

    except GuardianRuntimeError:
        raise
    except OSError:
        raise GuardianRuntimeError("Guardian state path must remain private.") from None
    return directory, database


def _poll_locking_is_available() -> bool:
    """Return whether this platform provides the required process lock."""

    return fcntl is not None and callable(getattr(fcntl, "flock", None))


def _poll_lock_descriptor_metadata(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError:
        raise GuardianRuntimeError(
            "Guardian poll lock is unavailable or unsafe."
        ) from None


def _validate_poll_lock_descriptor(descriptor: int) -> os.stat_result:
    """Require the exact private regular inode used for process locking."""

    metadata = _poll_lock_descriptor_metadata(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise GuardianRuntimeError("Guardian poll lock is unavailable or unsafe.")
    return metadata


def _poll_lock_inode_may_be_initializing(metadata: os.stat_result) -> bool:
    """Recognize only the restrictive-umask window of our exclusive creator."""

    mode = stat.S_IMODE(metadata.st_mode)
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
        and metadata.st_size == 0
        and mode != 0o600
        and mode & ~0o600 == 0
    )


def _poll_lock_path_may_be_initializing_or_ready(lock_path: Path) -> bool:
    """Recognize the safe states around a creator's final chmod."""

    try:
        metadata = lock_path.stat(follow_symlinks=False)
    except OSError:
        return False
    return _poll_lock_inode_may_be_initializing(metadata) or (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
        and metadata.st_size == 0
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _open_private_poll_lock(lock_path: Path) -> int:
    """Create an exact 0600 lock or open a previously validated lock inode."""

    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    deadline = time.monotonic() + _POLL_LOCK_INITIALIZATION_TIMEOUT_SECONDS
    while True:
        created = False
        try:
            try:
                descriptor = os.open(
                    lock_path,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(lock_path, flags)
        except FileNotFoundError:
            if time.monotonic() < deadline:
                time.sleep(_POLL_LOCK_RETRY_SECONDS)
                continue
            raise GuardianRuntimeError(
                "Guardian poll lock is unavailable or unsafe."
            ) from None
        except PermissionError:
            if (
                time.monotonic() < deadline
                and _poll_lock_path_may_be_initializing_or_ready(lock_path)
            ):
                time.sleep(_POLL_LOCK_RETRY_SECONDS)
                continue
            raise GuardianRuntimeError(
                "Guardian poll lock is unavailable or unsafe."
            ) from None
        except OSError:
            raise GuardianRuntimeError(
                "Guardian poll lock is unavailable or unsafe."
            ) from None

        try:
            if created:
                os.fchmod(descriptor, 0o600)
            _validate_poll_lock_descriptor(descriptor)
            return descriptor
        except GuardianRuntimeError:
            try:
                metadata = _poll_lock_descriptor_metadata(descriptor)
            except GuardianRuntimeError:
                os.close(descriptor)
                raise
            os.close(descriptor)
            if (
                not created
                and time.monotonic() < deadline
                and _poll_lock_inode_may_be_initializing(metadata)
            ):
                time.sleep(_POLL_LOCK_RETRY_SECONDS)
                continue
            raise
        except OSError:
            os.close(descriptor)
            raise GuardianRuntimeError(
                "Guardian poll lock is unavailable or unsafe."
            ) from None


def _preflight_poll_lock(state_directory: Path) -> None:
    """Validate an existing lock inode without creating or acquiring it."""

    if not _poll_locking_is_available():
        raise GuardianRuntimeError(
            "Guardian process locking is unavailable on this platform."
        )
    lock_path = state_directory / "poll.lock"
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(lock_path, flags)
    except FileNotFoundError:
        return
    except OSError:
        raise GuardianRuntimeError(
            "Guardian poll lock is unavailable or unsafe."
        ) from None
    try:
        _validate_poll_lock_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _probe_poll_lock_semantics(parent_directory: Path) -> None:
    """Prove exclusive contention and release on a disposable private inode."""

    try:
        with tempfile.TemporaryDirectory(
            prefix=".guardian-poll-lock-doctor-",
            dir=parent_directory,
        ) as raw_directory:
            probe_directory = Path(raw_directory)
            probe_directory.chmod(0o700)
            with _exclusive_poll_lock(probe_directory):
                try:
                    with _exclusive_poll_lock(probe_directory):
                        pass
                except _GuardianPollAlreadyRunning:
                    pass
                else:
                    raise GuardianRuntimeError(
                        "Guardian process locking is unavailable on this platform."
                    )
            with _exclusive_poll_lock(probe_directory):
                pass
    except GuardianRuntimeError:
        raise
    except OSError:
        raise GuardianRuntimeError(
            "Guardian process locking is unavailable on this platform."
        ) from None


@contextmanager
def _exclusive_poll_lock(state_directory: Path) -> Iterator[None]:
    """Hold one private, non-blocking process lock for the complete poll."""

    if not _poll_locking_is_available():
        raise GuardianRuntimeError(
            "Guardian process locking is unavailable on this platform."
        )
    assert fcntl is not None
    lock_path = state_directory / "poll.lock"
    descriptor = _open_private_poll_lock(lock_path)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise _GuardianPollAlreadyRunning from None
            raise GuardianRuntimeError(
                "Guardian poll lock is unavailable or unsafe."
            ) from None
        locked = True
        yield
    finally:
        if locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _validate_private_state_artifacts(database: Path) -> None:
    """Apply one redacted runtime boundary to all SQLite state artifacts."""

    try:
        _validate_sqlite_state_artifacts(database)
    except (OSError, ValueError):
        raise GuardianRuntimeError("Guardian state path must remain private.") from None


def _require_private_operator_directory(path: Path) -> Path:
    """Require a current-user, non-symlink 0700 directory and safe ancestors."""

    try:
        _validate_trusted_ancestors(path)
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise GuardianRuntimeError(
                "Guardian operator pipeline config is unavailable or unsafe."
            )
    except GuardianRuntimeError:
        raise
    except OSError:
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    return path


def _safe_operator_relative_path(raw_path: str) -> PurePosixPath:
    """Normalize one non-empty relative operator path without traversal."""

    if (
        not isinstance(raw_path, str)
        or not raw_path
        or len(raw_path) > 4096
        or not raw_path.isprintable()
        or "\\" in raw_path
        or "\x00" in raw_path
    ):
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        )
    path = PurePosixPath(raw_path)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(character in raw_path for character in "*?[]")
    ):
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        )
    return path


def _read_private_operator_file(
    path: Path,
    *,
    root: Path,
    required: bool,
) -> bytes | None:
    """Read one bounded 0600 file through a non-following descriptor."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        _require_private_operator_directory(current)

    try:
        leaf_metadata = path.lstat()
    except FileNotFoundError:
        if not required:
            return None
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    except OSError:
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    if stat.S_ISLNK(leaf_metadata.st_mode) or not stat.S_ISREG(
        leaf_metadata.st_mode
    ):
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if not required and not path.is_symlink():
            return None
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    except OSError:
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != leaf_metadata.st_dev
            or metadata.st_ino != leaf_metadata.st_ino
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_OPERATOR_PIPELINE_FILE_BYTES
        ):
            raise GuardianRuntimeError(
                "Guardian operator pipeline config is unavailable or unsafe."
            )
        chunks: list[bytes] = []
        remaining = _MAX_OPERATOR_PIPELINE_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_OPERATOR_PIPELINE_FILE_BYTES:
            raise GuardianRuntimeError(
                "Guardian operator pipeline config is unavailable or unsafe."
            )
        return content
    except GuardianRuntimeError:
        raise
    except OSError:
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    finally:
        os.close(descriptor)


def _decode_operator_yaml(content: bytes) -> Mapping[str, Any]:
    """Decode one bounded UTF-8 YAML object or fail through the redacted boundary."""

    try:
        payload = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        ) from None
    if not isinstance(payload, Mapping):
        raise GuardianRuntimeError(
            "Guardian operator pipeline config is unavailable or unsafe."
        )
    return payload


def _copy_private_bundle_file(root: Path, relative: PurePosixPath, content: bytes) -> Path:
    """Copy immutable snapshot bytes below a fresh private bundle root."""

    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        current.chmod(0o700)
    destination.write_bytes(content)
    destination.chmod(0o600)
    return destination


def _operator_bundle_digest(files: Mapping[str, bytes]) -> str:
    """Hash sorted path and raw-byte tuples without concatenation ambiguity."""

    digest = hashlib.sha256()
    for name in sorted(files):
        encoded_name = name.encode("utf-8")
        content = files[name]
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


@contextmanager
def _snapshot_operator_pipeline_configs(
    *,
    config: GuardianConfig,
    guardian_config_path: Path,
    state_directory: Path,
) -> Iterator[dict[str, PipelineConfigSnapshot]]:
    """Snapshot every operator pipeline config before external poll work starts."""

    operator_policies = tuple(
        policy
        for policy in config.repositories
        if policy.pipeline_config_source is PipelineConfigSource.OPERATOR
    )
    if not operator_policies:
        yield {}
        return

    operator_root = _require_private_operator_directory(guardian_config_path.parent)
    with tempfile.TemporaryDirectory(
        prefix="operator-pipeline-config-",
        dir=state_directory,
    ) as temporary_directory:
        snapshot_parent = Path(temporary_directory)
        snapshot_parent.chmod(0o700)
        snapshots: dict[str, PipelineConfigSnapshot] = {}
        for index, policy in enumerate(operator_policies):
            config_relative = _safe_operator_relative_path(
                policy.pipeline_config_path
            )
            live_config_path = operator_root.joinpath(*config_relative.parts)
            config_bytes = _read_private_operator_file(
                live_config_path,
                root=operator_root,
                required=True,
            )
            assert config_bytes is not None
            pipeline_config = _decode_operator_yaml(config_bytes)

            configured_glossary = pipeline_config.get("glossary_file_path")
            explicit_glossary = configured_glossary is not None
            if configured_glossary is not None and not isinstance(
                configured_glossary,
                str,
            ):
                raise GuardianRuntimeError(
                    "Guardian operator pipeline config is unavailable or unsafe."
                )
            glossary_relative_to_config = _safe_operator_relative_path(
                configured_glossary or "glossary.json"
            )
            glossary_relative = PurePosixPath(
                *config_relative.parent.parts,
                *glossary_relative_to_config.parts,
            )
            live_glossary_path = operator_root.joinpath(*glossary_relative.parts)
            glossary_bytes = _read_private_operator_file(
                live_glossary_path,
                root=operator_root,
                required=explicit_glossary,
            )
            if glossary_bytes is not None:
                try:
                    glossary_payload = json.loads(glossary_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise GuardianRuntimeError(
                        "Guardian operator pipeline config is unavailable or unsafe."
                    ) from None
                if not isinstance(glossary_payload, dict):
                    raise GuardianRuntimeError(
                        "Guardian operator pipeline config is unavailable or unsafe."
                    )

            bundle_files = {config_relative.as_posix(): config_bytes}
            if glossary_bytes is not None:
                bundle_files[glossary_relative.as_posix()] = glossary_bytes
            snapshot_root = snapshot_parent / f"repository-{index}"
            snapshot_root.mkdir(mode=0o700)
            snapshot_config = _copy_private_bundle_file(
                snapshot_root,
                config_relative,
                config_bytes,
            )
            if glossary_bytes is not None:
                _copy_private_bundle_file(
                    snapshot_root,
                    glossary_relative,
                    glossary_bytes,
                )
            snapshots[policy.base_repo] = PipelineConfigSnapshot(
                config_root=snapshot_root.resolve(),
                config_path=snapshot_config.resolve(),
                bundle_digest=_operator_bundle_digest(bundle_files),
            )
        yield snapshots


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
    if (
        config.mode in _WRITE_MODES
        and config.runtime.signing_format is SigningFormat.SSH
    ):
        try:
            require_absolute_trusted_executable(
                (config.runtime.signing_program,),
                field="runtime.signing_program",
            )
        except ExecutableTrustError:
            raise GuardianRuntimeError(
                "Guardian SSH signing authority is unavailable or unsafe."
            ) from None
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
        if config.runtime.signing_format is SigningFormat.SSH:
            canonical_ssh_fingerprint(key)  # type: ignore[arg-type]
            public_key = config.runtime.signing_public_key
            if public_key is None or not Path(public_key).is_absolute():
                raise ValueError
        else:
            canonical_signing_key(key)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise GuardianRuntimeError(
            "Guardian write modes require an explicit exact signing key fingerprint."
        ) from None


@contextmanager
def _snapshot_poll_signing_material(
    *,
    config: GuardianConfig,
    state_directory: Path,
) -> Iterator[SSHSigningMaterial | None]:
    """Freeze an SSH public identity before any external write-mode work."""

    if (
        config.mode not in _WRITE_MODES
        or config.runtime.signing_format is not SigningFormat.SSH
    ):
        yield None
        return
    try:
        assert config.runtime.signing_public_key is not None
        assert config.runtime.signing_key is not None
        with snapshot_ssh_signing_material(
            public_key_path=config.runtime.signing_public_key,
            expected_fingerprint=config.runtime.signing_key,
            signing_program=config.runtime.signing_program,
            temporary_root=state_directory,
        ) as material:
            yield material
    except SigningError:
        raise GuardianRuntimeError(
            "Guardian SSH signing identity is unavailable or unsafe."
        ) from None


def _build_controller(
    *,
    config: GuardianConfig,
    state: GuardianState,
    state_directory: Path,
    github_credential: SecretCommand,
    model_credential: SecretCommand | None,
    git_environment: Any,
    ssh_signing_material: SSHSigningMaterial | None = None,
    operator_pipeline_configs: Mapping[str, PipelineConfigSnapshot] | None = None,
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
        checkout_kwargs: dict[str, Any] = {
            "credential_environment": git_environment,
            "git_binary": config.runtime.git_executable,
            "signing_program": config.runtime.signing_program,
            "timeout_seconds": attempt_timeout,
        }
        if config.runtime.signing_format is SigningFormat.SSH:
            if ssh_signing_material is None:
                if config.mode in _WRITE_MODES:
                    raise GuardianRuntimeError(
                        "Guardian SSH signing identity is unavailable or unsafe."
                    )
                checkout_kwargs["signing_program"] = None
            else:
                checkout_kwargs.update(
                    signing_format=SigningFormat.SSH,
                    ssh_signing_material=ssh_signing_material,
                )
        return materialize_exact_checkout(revision, **checkout_kwargs)

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
        operator_pipeline_configs=operator_pipeline_configs,
    )


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _scheduled_poll_is_due(
    state: GuardianState,
    *,
    now: datetime,
    schedule: GuardianSchedule,
) -> bool:
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
    if latest_attempt is not None:
        details = getattr(latest_attempt, "details", {})
        recorded_local_date = (
            details.get("local_date") if isinstance(details, Mapping) else None
        )
        if isinstance(recorded_local_date, str):
            try:
                recorded_date = date.fromisoformat(recorded_local_date)
            except ValueError:
                pass
            else:
                if (
                    recorded_date.isoformat() == recorded_local_date
                    and recorded_date == now.date()
                ):
                    if details.get("scheduled") is True:
                        return False
                    recorded_local_minute = details.get("local_minute")
                    if (
                        isinstance(recorded_local_minute, int)
                        and not isinstance(recorded_local_minute, bool)
                        and 0 <= recorded_local_minute < 24 * 60
                    ):
                        scheduled_minute = schedule.hour * 60 + schedule.minute
                        if recorded_local_minute >= scheduled_minute:
                            return False
                        return is_run_due(
                            now=now,
                            last_success=None,
                            hour=schedule.hour,
                            minute=schedule.minute,
                        )
                return is_run_due(
                    now=now,
                    last_success=None,
                    hour=schedule.hour,
                    minute=schedule.minute,
                )
    return is_run_due(
        now=now,
        last_success=(
            latest_attempt.checked_at if latest_attempt is not None else None
        ),
        hour=schedule.hour,
        minute=schedule.minute,
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


def _poll_with_locked_state(
    *,
    config: GuardianConfig,
    resolved_config: Path,
    state_directory: Path,
    state_path: Path,
    scheduled: bool,
) -> int:
    """Execute a poll while the caller holds the config's process lock."""

    try:
        state_context = GuardianState(state_path)
    except Exception:
        raise GuardianRuntimeError("Guardian private state is unavailable.") from None

    try:
        with state_context as state:
            attempted_at = _local_now()
            if scheduled and not _scheduled_poll_is_due(
                state,
                now=attempted_at,
                schedule=config.schedule,
            ):
                return 0
            state.record_health(
                component=_POLL_ATTEMPT_COMPONENT,
                status="attempted",
                message="Guardian poll attempt started.",
                details={
                    "scheduled": scheduled,
                    "local_date": attempted_at.date().isoformat(),
                    "local_minute": attempted_at.hour * 60 + attempted_at.minute,
                },
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
            with _snapshot_poll_signing_material(
                config=config,
                state_directory=state_directory,
            ) as ssh_signing_material, _snapshot_operator_pipeline_configs(
                config=config,
                guardian_config_path=resolved_config,
                state_directory=state_directory,
            ) as operator_pipeline_configs:
                for repository, snapshot in operator_pipeline_configs.items():
                    state.record_health(
                        component="pipeline-config",
                        status="ok",
                        message="Operator pipeline config snapshot selected.",
                        details={
                            "repository": repository,
                            "bundle_digest": snapshot.bundle_digest,
                        },
                        checked_at=attempted_at,
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
                        ssh_signing_material=ssh_signing_material,
                        operator_pipeline_configs=operator_pipeline_configs,
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


def run_once(config_path: Path, scheduled: bool = False) -> int:
    """Load trusted policy and execute one finite, non-overlapping poll.

    Scheduled calls run at most once per local calendar day after the first
    attempted wake, regardless of its outcome. Manual invocations always poll.
    Credential contents and untrusted exception messages never cross this
    adapter's error boundary.
    """

    if not isinstance(scheduled, bool):
        raise GuardianRuntimeError("Guardian scheduled flag is invalid.")
    resolved_config = _resolved_config_path(config_path)
    config = load_trusted_guardian_config(resolved_config)
    _require_explicit_write_signing_key(config)
    state_directory, state_path = _prepare_private_state(resolved_config)
    try:
        with _exclusive_poll_lock(state_directory):
            _validate_private_state_artifacts(state_path)
            return _poll_with_locked_state(
                config=config,
                resolved_config=resolved_config,
                state_directory=state_directory,
                state_path=state_path,
                scheduled=scheduled,
            )
    except _GuardianPollAlreadyRunning:
        if scheduled:
            return 0
        raise GuardianRuntimeError("Guardian poll is already running.") from None


__all__: Sequence[str] = (
    "AuthenticatedGitHubSnapshotProvider",
    "GuardianRuntimeError",
    "load_trusted_guardian_config",
    "run_once",
)
