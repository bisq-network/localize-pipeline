"""Typed data exchanged by the localization PR guardian."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import ClassVar, Mapping


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


class SigningFormat(str, Enum):
    """Commit-signature format used by the trusted publication broker."""

    OPENPGP = "openpgp"
    SSH = "ssh"


class HistoricalCheckScope(str, Enum):
    """Authority covered by one completed closed-PR assessment."""

    ASSESSMENT = "assessment"
    PREVENTION = "prevention"
    REMEDIATION = "remediation"


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_BOUND_STRING_BYTES = 4096
_MAX_PREVENTION_PATH_GLOBS = 100
_MAX_PREVENTION_TEST_COMMANDS = 64
_MAX_PREVENTION_ARGV_ITEMS = 256
_MAX_PREVENTION_CHANGED_FILES = 100
_PREVENTION_BRANCH_SUFFIX_CHARS = 77
_MAX_RECURRENCE_CANDIDATES = 100
_MAX_RECURRENCE_EVIDENCE_IDS = 100
_MAX_RUNTIME_COMMAND_ITEMS = 32
_LOCALE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SAFE_PATH_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*?@+\-]+$")
_SAFE_BRANCH_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*?@+\-]+$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")
_CODEX_REASONING_EFFORTS = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CREDENTIAL_OPTION_RE = re.compile(
    r"^--(?:api[-_]?key|credential|password|secret|token)(?:=|$)",
    re.IGNORECASE,
)
_SHELL_EXECUTABLES = frozenset(
    {
        "bash",
        "cmd",
        "dash",
        "env",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "zsh",
    }
)


def _is_bounded_single_line(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isprintable()
        and len(value.encode("utf-8")) <= _MAX_BOUND_STRING_BYTES
    )


def pipeline_config_bundle_digest(files: Mapping[str, bytes]) -> str:
    """Hash exact path/raw-byte pairs without concatenation ambiguity."""

    if not files:
        raise ValueError("Pipeline config bundle must not be empty.")
    entries = tuple(files.items())
    if any(
        not isinstance(name, str) or not name or not isinstance(content, bytes)
        for name, content in entries
    ):
        raise ValueError("Pipeline config bundle entries must be path/byte pairs.")
    digest = hashlib.sha256()
    for name, content in sorted(entries):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_repository_name(value: str, *, field_name: str) -> None:
    if (
        not _is_bounded_single_line(value)
        or not _REPOSITORY_RE.fullmatch(value)
        or any(component in {".", ".."} for component in value.split("/"))
    ):
        raise ValueError(f"{field_name} must use canonical owner/name form.")


def _validate_runtime_command(value: object, *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a bounded argv sequence.")
    try:
        argv = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError(f"{field_name} must be a bounded argv sequence.") from None
    if not 1 <= len(argv) <= _MAX_RUNTIME_COMMAND_ITEMS:
        raise ValueError(
            f"{field_name} must contain between 1 and "
            f"{_MAX_RUNTIME_COMMAND_ITEMS} arguments."
        )
    if any(not _is_bounded_single_line(argument) for argument in argv):
        raise ValueError(
            f"{field_name} arguments must be non-empty single-line strings of at "
            "most 4096 UTF-8 bytes."
        )

    executable_path = PurePosixPath(argv[0])
    if ".." in executable_path.parts:
        raise ValueError(f"{field_name}.0 must not traverse parent directories.")
    executable = executable_path.name.casefold()
    if executable in _SHELL_EXECUTABLES:
        raise ValueError(f"{field_name} must not invoke a shell wrapper.")
    for index, argument in enumerate(argv):
        if _ENV_ASSIGNMENT_RE.match(argument) or _CREDENTIAL_OPTION_RE.match(argument):
            raise ValueError(
                f"{field_name}.{index} must not contain credentials or environment "
                "assignments."
            )
    if executable.startswith("python") and "-c" in argv[1:]:
        raise ValueError(f"{field_name} must not contain an interpreter command string.")
    if executable in {"node", "perl", "ruby"} and any(
        argument in {"-e", "--eval"} for argument in argv[1:]
    ):
        raise ValueError(f"{field_name} must not contain an interpreter command string.")
    if executable == "php" and "-r" in argv[1:]:
        raise ValueError(f"{field_name} must not contain an interpreter command string.")
    return argv


def _validate_codex_home(value: object) -> None:
    if not _is_bounded_single_line(value):
        raise ValueError(
            "codex_home must be a non-empty single-line value of at most 4096 "
            "UTF-8 bytes."
        )
    assert isinstance(value, str)
    if "\\" in value or "\x00" in value or any(char in value for char in "*?[]"):
        raise ValueError("codex_home must be an absolute or ~/ POSIX directory.")
    candidate = value[2:] if value.startswith("~/") else value
    path = PurePosixPath(candidate)
    if (
        not candidate
        or ".." in path.parts
        or (not value.startswith("~/") and not path.is_absolute())
    ):
        raise ValueError("codex_home must be an absolute or ~/ POSIX directory.")


def _validate_branch_scope(
    value: object,
    *,
    field_name: str,
    prefix: bool = False,
) -> None:
    if not _is_bounded_single_line(value):
        raise ValueError(
            f"{field_name} must be a non-empty single-line Git branch of at most "
            "4096 UTF-8 bytes."
        )
    assert isinstance(value, str)
    candidate = f"{value}candidate" if prefix else value
    if (
        len(value) > 255
        or value.startswith("refs/")
        or not _SAFE_BRANCH_RE.fullmatch(candidate)
        or "//" in candidate
        or ".." in candidate
        or "@{" in candidate
        or candidate.endswith(".")
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in candidate.split("/")
        )
    ):
        kind = "branch prefix" if prefix else "branch"
        raise ValueError(f"{field_name} must be a safe Git {kind}.")


def _validate_branch_glob(value: object, *, field_name: str) -> None:
    if not _is_bounded_single_line(value):
        raise ValueError(f"{field_name} must be a bounded branch-name glob.")
    assert isinstance(value, str)
    candidate = value.replace("*", "x").replace("?", "x")
    if (
        not _SAFE_BRANCH_GLOB_RE.fullmatch(value)
        or value.startswith(("-", "refs/"))
    ):
        raise ValueError(f"{field_name} must be a safe branch-name glob.")
    _validate_branch_scope(candidate, field_name=field_name)


def _validate_path_glob(value: object, *, field_name: str) -> None:
    if not _is_bounded_single_line(value):
        raise ValueError(f"{field_name} must be a bounded relative POSIX path glob.")
    assert isinstance(value, str)
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or value in {"", "."}
        or ".." in path.parts
        or not _SAFE_PATH_GLOB_RE.fullmatch(value)
        or "//" in value
        or value.startswith("-")
        or ".git" in path.parts
    ):
        raise ValueError(f"{field_name} must be a safe relative POSIX path glob.")


def _validate_plain_relative_path(value: object, *, field_name: str) -> None:
    if not _is_bounded_single_line(value):
        raise ValueError(f"{field_name} must be one exact relative POSIX path.")
    assert isinstance(value, str)
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or value in {"", "."}
        or ".." in path.parts
        or any(character in value for character in "*?[]")
        or any(part.casefold() == ".git" for part in path.parts)
    ):
        raise ValueError(f"{field_name} must be one exact relative POSIX path.")


def _bounded_tuple(value: object, *, field_name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a non-empty sequence.")
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError(f"{field_name} must be a non-empty sequence.") from None
    if not values:
        raise ValueError(f"{field_name} must be a non-empty sequence.")
    return values


def _validate_absolute_argv(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a bounded argv sequence.")
    try:
        argv = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError(f"{field_name} must be a bounded argv sequence.") from None
    if not 1 <= len(argv) <= _MAX_PREVENTION_ARGV_ITEMS:
        raise ValueError(
            f"{field_name} must contain between 1 and "
            f"{_MAX_PREVENTION_ARGV_ITEMS} arguments."
        )
    if any(not _is_bounded_single_line(argument) for argument in argv):
        raise ValueError(
            f"{field_name} arguments must be non-empty single-line strings of at "
            "most 4096 UTF-8 bytes."
        )
    executable_path = PurePosixPath(argv[0])
    if not executable_path.is_absolute() or ".." in executable_path.parts:
        raise ValueError(f"{field_name}.0 must be an absolute POSIX executable path.")
    executable = executable_path.name.casefold()
    if executable in _SHELL_EXECUTABLES:
        raise ValueError(f"{field_name} must not invoke a shell wrapper.")
    for index, argument in enumerate(argv):
        if _ENV_ASSIGNMENT_RE.match(argument) or _CREDENTIAL_OPTION_RE.match(argument):
            raise ValueError(
                f"{field_name}.{index} must not contain credentials or environment "
                "assignments."
            )
    if executable.startswith("python") and "-c" in argv[1:]:
        raise ValueError(f"{field_name} must not contain an interpreter command string.")
    if executable in {"node", "perl", "ruby"} and any(
        argument in {"-e", "--eval"} for argument in argv[1:]
    ):
        raise ValueError(f"{field_name} must not contain an interpreter command string.")
    if executable == "php" and "-r" in argv[1:]:
        raise ValueError(f"{field_name} must not contain an interpreter command string.")
    return argv


@dataclass(frozen=True)
class GuardianLimits:
    """Resource and change limits for one bounded poll and its feedback runs."""

    run_timeout_seconds: int = 3600
    max_attempts: int = 2
    max_value_edits_per_run: int = 20
    max_prevention_drafts_per_run: int = 1
    max_model_calls_per_day: int = 2
    daily_cost_limit_usd: float | None = None
    model_call_reservation_usd: float | None = None
    min_apply_confidence: float = 0.9
    raw_retention_days: int = 90
    max_remediation_drafts_per_run: int = 0

    def __post_init__(self) -> None:
        integer_bounds = (
            ("run_timeout_seconds", self.run_timeout_seconds, 1, None),
            ("max_attempts", self.max_attempts, 1, 2),
            ("max_value_edits_per_run", self.max_value_edits_per_run, 0, 100),
            (
                "max_prevention_drafts_per_run",
                self.max_prevention_drafts_per_run,
                0,
                None,
            ),
            (
                "max_remediation_drafts_per_run",
                self.max_remediation_drafts_per_run,
                0,
                None,
            ),
            ("max_model_calls_per_day", self.max_model_calls_per_day, 1, None),
            ("raw_retention_days", self.raw_retention_days, 1, None),
        )
        for field_name, value, minimum, maximum in integer_bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
                or (maximum is not None and value > maximum)
            ):
                bound = (
                    f"between {minimum} and {maximum}"
                    if maximum is not None
                    else f"at least {minimum}"
                )
                raise ValueError(f"{field_name} must be an integer {bound}.")

        for field_name, value in (
            ("daily_cost_limit_usd", self.daily_cost_limit_usd),
            ("model_call_reservation_usd", self.model_call_reservation_usd),
        ):
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a finite positive number.")
            object.__setattr__(self, field_name, float(value))

        confidence = self.min_apply_confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError(
                "min_apply_confidence must be a finite number between 0 and 1."
            )
        object.__setattr__(self, "min_apply_confidence", float(confidence))

        has_daily_limit = self.daily_cost_limit_usd is not None
        has_reservation = self.model_call_reservation_usd is not None
        if has_daily_limit != has_reservation:
            raise ValueError(
                "daily_cost_limit_usd and model_call_reservation_usd must be set "
                "together."
            )
        if (
            self.daily_cost_limit_usd is not None
            and self.model_call_reservation_usd is not None
            and self.model_call_reservation_usd > self.daily_cost_limit_usd
        ):
            raise ValueError(
                "model_call_reservation_usd must not exceed daily_cost_limit_usd."
            )


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
    signing_format: SigningFormat = SigningFormat.OPENPGP
    signing_public_key: str | None = None

    def __post_init__(self) -> None:
        try:
            auth_mode = CodexAuthMode(self.codex_auth_mode)
        except (TypeError, ValueError):
            raise ValueError("codex_auth_mode must be chatgpt or api-key.") from None
        try:
            signing_format = SigningFormat(self.signing_format)
        except (TypeError, ValueError):
            raise ValueError("signing_format must be openpgp or ssh.") from None
        object.__setattr__(self, "codex_auth_mode", auth_mode)
        object.__setattr__(self, "signing_format", signing_format)

        for field_name, value in (
            ("codex_model", self.codex_model),
            ("codex_executable", self.codex_executable),
            ("git_executable", self.git_executable),
            ("signing_program", self.signing_program),
        ):
            if not _is_bounded_single_line(value):
                raise ValueError(
                    f"{field_name} must be a non-empty single-line value of at most "
                    "4096 UTF-8 bytes."
                )
        if (
            not isinstance(self.codex_reasoning_effort, str)
            or self.codex_reasoning_effort not in _CODEX_REASONING_EFFORTS
        ):
            raise ValueError(
                "codex_reasoning_effort must be low, medium, high, xhigh, max, or "
                "ultra."
            )
        _validate_codex_home(self.codex_home)

        github_command = _validate_runtime_command(
            self.github_token_command,
            field_name="github_token_command",
        )
        if isinstance(self.codex_api_key_command, (str, bytes)) or not isinstance(
            self.codex_api_key_command,
            Sequence,
        ):
            raise ValueError("codex_api_key_command must be an argv sequence.")
        try:
            raw_api_command = tuple(self.codex_api_key_command)
        except TypeError:
            raise ValueError("codex_api_key_command must be an argv sequence.") from None
        api_command = (
            _validate_runtime_command(
                raw_api_command,
                field_name="codex_api_key_command",
            )
            if raw_api_command
            else ()
        )
        object.__setattr__(self, "github_token_command", github_command)
        object.__setattr__(self, "codex_api_key_command", api_command)
        if auth_mode is CodexAuthMode.CHATGPT and api_command:
            raise ValueError(
                "codex_api_key_command is only valid with codex_auth_mode api-key."
            )
        if auth_mode is CodexAuthMode.API_KEY and not api_command:
            raise ValueError(
                "codex_api_key_command is required with codex_auth_mode api-key."
            )

        signing_key = self.signing_key
        if signing_key is not None and not _is_bounded_single_line(signing_key):
            raise ValueError(
                "signing_key must be a non-empty single-line value of at most 4096 "
                "UTF-8 bytes."
            )
        signing_public_key = self.signing_public_key
        if signing_public_key is not None and not _is_bounded_single_line(
            signing_public_key
        ):
            raise ValueError(
                "signing_public_key must be a non-empty single-line value of at most "
                "4096 UTF-8 bytes."
            )

        if signing_format is SigningFormat.SSH:
            if signing_key is None:
                raise ValueError("signing_key is required with signing_format ssh.")
            if signing_public_key is None:
                raise ValueError(
                    "signing_public_key is required with signing_format ssh."
                )
            public_key_path = PurePosixPath(signing_public_key)
            if (
                not public_key_path.is_absolute()
                or ".." in public_key_path.parts
                or any(character in signing_public_key for character in "*?[]\\")
            ):
                raise ValueError(
                    "signing_public_key must be an absolute POSIX file path."
                )
            signing_program_path = PurePosixPath(self.signing_program)
            if (
                not signing_program_path.is_absolute()
                or ".." in signing_program_path.parts
            ):
                raise ValueError(
                    "signing_program must be an absolute POSIX executable path with "
                    "signing_format ssh."
                )
        elif signing_public_key is not None:
            raise ValueError("signing_public_key is only valid with signing_format ssh.")

        if signing_key is not None:
            # Keep direct typed construction aligned with the strict YAML parser's
            # canonical exact-key representation.
            from localize.guardian.signing import (
                canonical_signing_key,
                canonical_ssh_fingerprint,
            )

            canonical_key = (
                canonical_ssh_fingerprint(signing_key)
                if signing_format is SigningFormat.SSH
                else canonical_signing_key(signing_key)
            )
            object.__setattr__(self, "signing_key", canonical_key)


@dataclass(frozen=True)
class TrustedActor:
    """A GitHub actor pinned by immutable numeric identity."""

    login: str
    id: int
    type: str

    def __post_init__(self) -> None:
        if not _is_bounded_single_line(self.login):
            raise ValueError("Trusted actor login must be a bounded single-line value.")
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id <= 0:
            raise ValueError("Trusted actor id must be positive.")
        if not isinstance(self.type, str) or self.type not in {
            "User",
            "Bot",
            "Organization",
        }:
            raise ValueError("Trusted actor type must be User, Bot, or Organization.")


@dataclass(frozen=True)
class AllowedHeadRepository:
    """One exact GitHub repository in which Guardian may advance a PR branch."""

    full_name: str
    id: int

    def __post_init__(self) -> None:
        _validate_repository_name(self.full_name, field_name="full_name")
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id <= 0:
            raise ValueError("Allowed head repository id must be positive.")


@dataclass(frozen=True)
class ExactRepository:
    """A GitHub repository pinned by full name and immutable numeric ID."""

    full_name: str
    id: int

    def __post_init__(self) -> None:
        _validate_repository_name(self.full_name, field_name="full_name")
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id <= 0:
            raise ValueError("Exact repository id must be positive.")


@dataclass(frozen=True)
class PreventionPolicy:
    """Explicit authority for preparing one bounded prevention draft."""

    target_repository: ExactRepository
    target_base_branch: str
    push_repository: ExactRepository
    push_branch_prefix: str
    publication_actor: TrustedActor
    allowed_code_path_globs: tuple[str, ...]
    allowed_test_path_globs: tuple[str, ...]
    focused_test_argv: tuple[tuple[str, ...], ...]
    sandbox_argv_prefix: tuple[str, ...]
    max_changed_files: int
    max_changed_bytes: int
    private_target_model_opt_in: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.target_repository, ExactRepository):
            raise ValueError("target_repository must be an ExactRepository.")
        if not isinstance(self.push_repository, ExactRepository):
            raise ValueError("push_repository must be an ExactRepository.")
        _validate_branch_scope(
            self.target_base_branch,
            field_name="target_base_branch",
        )
        _validate_branch_scope(
            self.push_branch_prefix,
            field_name="push_branch_prefix",
            prefix=True,
        )
        if len(self.push_branch_prefix) + _PREVENTION_BRANCH_SUFFIX_CHARS > 255:
            raise ValueError(
                "push_branch_prefix must leave room for the generated branch identity."
            )
        if not isinstance(self.allowed_code_path_globs, Sequence) or isinstance(
            self.allowed_code_path_globs,
            (str, bytes),
        ):
            raise ValueError("allowed code path globs must be a bounded sequence.")
        if not isinstance(self.allowed_test_path_globs, Sequence) or isinstance(
            self.allowed_test_path_globs,
            (str, bytes),
        ):
            raise ValueError("allowed test path globs must be a bounded sequence.")
        if not isinstance(self.focused_test_argv, Sequence) or isinstance(
            self.focused_test_argv,
            (str, bytes),
        ):
            raise ValueError("focused test commands must be a bounded sequence.")
        if not isinstance(self.sandbox_argv_prefix, Sequence) or isinstance(
            self.sandbox_argv_prefix,
            (str, bytes),
        ):
            raise ValueError("sandbox argv must be a bounded sequence.")
        try:
            code_globs = tuple(self.allowed_code_path_globs)
            test_globs = tuple(self.allowed_test_path_globs)
            raw_commands = tuple(self.focused_test_argv)
            sandbox_argv = tuple(self.sandbox_argv_prefix)
            focused_test_argv = tuple(
                _validate_absolute_argv(
                    argv,
                    field_name=f"focused_test_argv.{index}",
                )
                for index, argv in enumerate(raw_commands)
            )
            sandbox_argv = _validate_absolute_argv(
                sandbox_argv,
                field_name="sandbox_argv_prefix",
            )
        except TypeError:
            raise ValueError(
                "prevention collections must contain bounded string sequences."
            ) from None
        object.__setattr__(
            self,
            "allowed_code_path_globs",
            code_globs,
        )
        object.__setattr__(
            self,
            "allowed_test_path_globs",
            test_globs,
        )
        object.__setattr__(
            self,
            "focused_test_argv",
            focused_test_argv,
        )
        object.__setattr__(
            self,
            "sandbox_argv_prefix",
            sandbox_argv,
        )
        for label, values in (
            ("allowed code path globs", self.allowed_code_path_globs),
            ("allowed test path globs", self.allowed_test_path_globs),
        ):
            if not 1 <= len(values) <= _MAX_PREVENTION_PATH_GLOBS:
                raise ValueError(f"{label} exceed their collection bound.")
            if any(not _is_bounded_single_line(value) for value in values):
                raise ValueError(
                    f"{label} must contain strings of at most 4096 UTF-8 bytes."
                )
        if len(code_globs) != len(set(code_globs)):
            raise ValueError("allowed_code_path_globs must not contain duplicates.")
        if len(test_globs) != len(set(test_globs)):
            raise ValueError("allowed_test_path_globs must not contain duplicates.")
        for index, path_glob in enumerate(code_globs):
            _validate_path_glob(
                path_glob,
                field_name=f"allowed_code_path_globs.{index}",
            )
        for index, path_glob in enumerate(test_globs):
            _validate_path_glob(
                path_glob,
                field_name=f"allowed_test_path_globs.{index}",
            )
        if set(code_globs) & set(test_globs):
            raise ValueError("code and test path glob allowlists must not overlap.")
        if not 1 <= len(self.focused_test_argv) <= _MAX_PREVENTION_TEST_COMMANDS:
            raise ValueError("focused test command count exceeds its collection bound.")
        if len(self.focused_test_argv) != len(set(self.focused_test_argv)):
            raise ValueError("focused_test_argv must not contain duplicates.")
        if (
            not isinstance(self.publication_actor, TrustedActor)
            or self.publication_actor.type != "User"
        ):
            raise ValueError("publication_actor must be a User identity.")
        same_name = (
            self.target_repository.full_name.casefold()
            == self.push_repository.full_name.casefold()
        )
        same_id = self.target_repository.id == self.push_repository.id
        if same_name != same_id:
            raise ValueError(
                "target_repository and push_repository have an ambiguous identity."
            )
        if (
            isinstance(self.max_changed_files, bool)
            or not isinstance(self.max_changed_files, int)
            or not 1 <= self.max_changed_files <= _MAX_PREVENTION_CHANGED_FILES
        ):
            raise ValueError("max_changed_files must be an integer between 1 and 100.")
        if (
            isinstance(self.max_changed_bytes, bool)
            or not isinstance(self.max_changed_bytes, int)
            or self.max_changed_bytes <= 0
        ):
            raise ValueError("max_changed_bytes must be positive.")
        if not isinstance(self.private_target_model_opt_in, bool):
            raise ValueError("private_target_model_opt_in must be a boolean.")


@dataclass(frozen=True)
class HistoricalRemediationPolicy:
    """Explicit authority to publish one current-base correction draft."""

    push_repository: ExactRepository
    push_branch_prefix: str
    publication_actor: TrustedActor

    def __post_init__(self) -> None:
        if not isinstance(self.push_repository, ExactRepository):
            raise ValueError("push_repository must be an ExactRepository.")
        if (
            not isinstance(self.publication_actor, TrustedActor)
            or self.publication_actor.type != "User"
        ):
            raise ValueError("publication_actor must be a User identity.")
        prefix = self.push_branch_prefix
        candidate = f"{prefix}{'0' * 64}" if isinstance(prefix, str) else ""
        if (
            not prefix
            or len(candidate) > 255
            or prefix.startswith("refs/")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\-]*", candidate)
            or "//" in candidate
            or ".." in candidate
            or "@{" in candidate
            or candidate.endswith(".")
            or any(
                component.startswith(".") or component.endswith(".lock")
                for component in candidate.split("/")
            )
        ):
            raise ValueError("push_branch_prefix must be a safe Git branch prefix.")


@dataclass(frozen=True)
class ClosedPrBackfillPolicy:
    """Bounded opt-in discovery of previously unchecked closed pull requests."""

    MAX_LOOKBACK_DAYS: ClassVar[int] = 3650
    MAX_PRS_PER_POLL: ClassVar[int] = 100

    lookback_days: int
    max_prs_per_poll: int
    remediation: HistoricalRemediationPolicy | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.lookback_days, bool)
            or not isinstance(self.lookback_days, int)
            or not 1 <= self.lookback_days <= self.MAX_LOOKBACK_DAYS
        ):
            raise ValueError(
                "lookback_days must be an integer between 1 and "
                f"{self.MAX_LOOKBACK_DAYS}."
            )
        if (
            isinstance(self.max_prs_per_poll, bool)
            or not isinstance(self.max_prs_per_poll, int)
            or not 1 <= self.max_prs_per_poll <= self.MAX_PRS_PER_POLL
        ):
            raise ValueError(
                "max_prs_per_poll must be an integer between 1 and "
                f"{self.MAX_PRS_PER_POLL}."
            )
        if self.remediation is not None and not isinstance(
            self.remediation,
            HistoricalRemediationPolicy,
        ):
            raise ValueError(
                "remediation must be a HistoricalRemediationPolicy or None."
            )


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
    closed_pr_backfill: ClosedPrBackfillPolicy | None = None
    publication_actor: TrustedActor | None = None

    def __post_init__(self) -> None:
        _validate_repository_name(self.base_repo, field_name="base_repo")
        if (
            isinstance(self.base_repo_id, bool)
            or not isinstance(self.base_repo_id, int)
            or self.base_repo_id <= 0
        ):
            raise ValueError("Base repository id must be positive.")
        _validate_branch_scope(self.base_branch, field_name="base_branch")
        _validate_plain_relative_path(
            self.pipeline_config_path,
            field_name="pipeline_config_path",
        )
        if not _is_bounded_single_line(self.source_locale) or not _LOCALE_RE.fullmatch(
            self.source_locale
        ):
            raise ValueError("source_locale must be a canonical locale identifier.")
        if not isinstance(self.private_repo_model_opt_in, bool):
            raise ValueError("private_repo_model_opt_in must be a boolean.")
        if self.publication_actor is not None and (
            not isinstance(self.publication_actor, TrustedActor)
            or self.publication_actor.type != "User"
        ):
            raise ValueError(
                "publication_actor must be a User identity, or None."
            )

        allowed_pr_authors = _bounded_tuple(
            self.allowed_pr_authors,
            field_name="allowed_pr_authors",
        )
        allowed_head_owners = _bounded_tuple(
            self.allowed_head_owners,
            field_name="allowed_head_owners",
        )
        head_repositories = _bounded_tuple(
            self.allowed_head_repositories,
            field_name="allowed_head_repositories",
        )
        for field_name, actors, allowed_types in (
            ("allowed_pr_authors", allowed_pr_authors, {"User", "Bot"}),
            (
                "allowed_head_owners",
                allowed_head_owners,
                {"User", "Bot", "Organization"},
            ),
        ):
            if any(
                not isinstance(actor, TrustedActor) or actor.type not in allowed_types
                for actor in actors
            ):
                raise ValueError(
                    f"{field_name} must contain only TrustedActor values with allowed "
                    "roles."
                )
            actor_ids = tuple(actor.id for actor in actors)
            if len(actor_ids) != len(set(actor_ids)):
                raise ValueError(f"{field_name} must not contain duplicate actor IDs.")
        if any(
            not isinstance(repository, AllowedHeadRepository)
            for repository in head_repositories
        ):
            raise ValueError(
                "allowed_head_repositories must contain AllowedHeadRepository values."
            )
        head_ids = tuple(repository.id for repository in head_repositories)
        head_names = tuple(
            repository.full_name.casefold() for repository in head_repositories
        )
        if len(head_ids) != len(set(head_ids)) or len(head_names) != len(
            set(head_names)
        ):
            raise ValueError(
                "allowed_head_repositories must not contain duplicate identities."
            )

        branch_globs = _bounded_tuple(
            self.allowed_branch_globs,
            field_name="allowed_branch_globs",
        )
        path_globs = _bounded_tuple(
            self.allowed_path_globs,
            field_name="allowed_path_globs",
        )
        for index, branch_glob in enumerate(branch_globs):
            _validate_branch_glob(
                branch_glob,
                field_name=f"allowed_branch_globs.{index}",
            )
        for index, path_glob in enumerate(path_globs):
            _validate_path_glob(
                path_glob,
                field_name=f"allowed_path_globs.{index}",
            )
        if len(branch_globs) != len(set(branch_globs)):
            raise ValueError("allowed_branch_globs must not contain duplicates.")
        if len(path_globs) != len(set(path_globs)):
            raise ValueError("allowed_path_globs must not contain duplicates.")

        if not isinstance(self.trusted_reviewers, Mapping) or not self.trusted_reviewers:
            raise ValueError("trusted_reviewers must be a non-empty locale mapping.")
        if not isinstance(self.trusted_bots, Mapping):
            raise ValueError("trusted_bots must be a locale mapping.")

        def trusted_map(
            source: Mapping[str, tuple[TrustedActor, ...]],
            *,
            field_name: str,
            actor_type: str,
        ) -> dict[str, tuple[TrustedActor, ...]]:
            result: dict[str, tuple[TrustedActor, ...]] = {}
            for locale, raw_accounts in source.items():
                if not _is_bounded_single_line(locale) or not _LOCALE_RE.fullmatch(
                    locale
                ):
                    raise ValueError(
                        f"{field_name} keys must be canonical locale identifiers."
                    )
                accounts = _bounded_tuple(
                    raw_accounts,
                    field_name=f"{field_name}.{locale}",
                )
                if any(
                    not isinstance(actor, TrustedActor) or actor.type != actor_type
                    for actor in accounts
                ):
                    raise ValueError(
                        f"{field_name}.{locale} must contain only {actor_type} actors."
                    )
                actor_ids = tuple(actor.id for actor in accounts)
                if len(actor_ids) != len(set(actor_ids)):
                    raise ValueError(
                        f"{field_name}.{locale} must not contain duplicate actor IDs."
                    )
                result[locale] = tuple(accounts)  # type: ignore[assignment]
            return result

        reviewers = trusted_map(
            self.trusted_reviewers,
            field_name="trusted_reviewers",
            actor_type="User",
        )
        bots = trusted_map(
            self.trusted_bots,
            field_name="trusted_bots",
            actor_type="Bot",
        )
        for locale in set(reviewers) | set(bots):
            combined_ids = tuple(
                actor.id for actor in (*reviewers.get(locale, ()), *bots.get(locale, ()))
            )
            if len(combined_ids) != len(set(combined_ids)):
                raise ValueError(
                    f"trusted actor ID {locale!r} must be unique across reviewer and "
                    "bot roles."
                )

        object.__setattr__(self, "allowed_pr_authors", tuple(allowed_pr_authors))
        object.__setattr__(self, "allowed_head_owners", tuple(allowed_head_owners))
        object.__setattr__(self, "allowed_head_repositories", tuple(head_repositories))
        object.__setattr__(self, "allowed_branch_globs", tuple(branch_globs))
        object.__setattr__(self, "allowed_path_globs", tuple(path_globs))
        object.__setattr__(self, "trusted_reviewers", MappingProxyType(reviewers))
        object.__setattr__(self, "trusted_bots", MappingProxyType(bots))
        try:
            pipeline_config_source = PipelineConfigSource(self.pipeline_config_source)
        except (TypeError, ValueError):
            raise ValueError("pipeline_config_source must be base or operator.") from None
        object.__setattr__(self, "pipeline_config_source", pipeline_config_source)
        if self.prevention is not None and not isinstance(
            self.prevention,
            PreventionPolicy,
        ):
            raise ValueError("prevention must be a PreventionPolicy or None.")
        if self.closed_pr_backfill is not None and not isinstance(
            self.closed_pr_backfill,
            ClosedPrBackfillPolicy,
        ):
            raise ValueError(
                "closed_pr_backfill must be a ClosedPrBackfillPolicy or None."
            )
        remediation = (
            self.closed_pr_backfill.remediation
            if self.closed_pr_backfill is not None
            else None
        )
        if remediation is not None:
            same_name = (
                remediation.push_repository.full_name.casefold()
                == self.base_repo.casefold()
            )
            same_id = remediation.push_repository.id == self.base_repo_id
            if same_name != same_id:
                raise ValueError(
                    "Historical remediation has an ambiguous repository identity."
                )
            if not any(
                repository.id == remediation.push_repository.id
                and repository.full_name.casefold()
                == remediation.push_repository.full_name.casefold()
                for repository in self.allowed_head_repositories
            ):
                raise ValueError(
                    "Historical remediation push_repository must be an allowed "
                    "head repository."
                )
            generated_namespace = f"{remediation.push_branch_prefix}*"
            if generated_namespace not in self.allowed_branch_globs:
                raise ValueError(
                    "Historical remediation push_branch_prefix must create an "
                    "allowed head branch."
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
            (
                actor
                for actor in self.trusted_reviewers_for(locale)
                if actor.id == actor_id
            ),
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

    def __post_init__(self) -> None:
        try:
            mode = GuardianMode(self.mode)
        except (TypeError, ValueError):
            raise ValueError("mode must be a GuardianMode.") from None
        object.__setattr__(self, "mode", mode)
        repositories = _bounded_tuple(self.repositories, field_name="repositories")
        if any(not isinstance(policy, RepositoryPolicy) for policy in repositories):
            raise ValueError("repositories must contain RepositoryPolicy values.")
        normalized_names = tuple(policy.base_repo.casefold() for policy in repositories)
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("repositories must not contain duplicate base repositories.")
        object.__setattr__(self, "repositories", tuple(repositories))
        if not isinstance(self.limits, GuardianLimits):
            raise ValueError("limits must be a GuardianLimits value.")
        if not isinstance(self.runtime, GuardianRuntime):
            raise ValueError("runtime must be a GuardianRuntime value.")
        if not isinstance(self.schedule, GuardianSchedule):
            raise ValueError("schedule must be a GuardianSchedule value.")
        if mode is GuardianMode.PROPOSE_PREVENTION and any(
            policy.prevention is None for policy in self.repositories
        ):
            raise ValueError(
                "Every repository requires a prevention policy in propose-prevention "
                "mode."
            )
        if mode in {
            GuardianMode.APPLY_OWNED_TRANSLATIONS,
            GuardianMode.PROPOSE_PREVENTION,
        } and any(policy.publication_actor is None for policy in self.repositories):
            raise ValueError(
                "Every repository requires a publication_actor in write modes."
            )
        actor_identities = {
            (actor.id, actor.type) for actor in self.enabled_publication_actors
        }
        if len(actor_identities) > 1:
            raise ValueError(
                "Enabled publication policies must use one GitHub actor identity."
            )

    @property
    def enabled_publication_actors(self) -> tuple[TrustedActor, ...]:
        """Return exact actors for remote publication paths enabled this run."""

        actors: list[TrustedActor] = []
        if self.mode in {
            GuardianMode.APPLY_OWNED_TRANSLATIONS,
            GuardianMode.PROPOSE_PREVENTION,
        }:
            actors.extend(
                policy.publication_actor
                for policy in self.repositories
                if policy.publication_actor is not None
            )
        if (
            self.mode is GuardianMode.PROPOSE_PREVENTION
            and self.limits.max_prevention_drafts_per_run > 0
        ):
            actors.extend(
                policy.prevention.publication_actor
                for policy in self.repositories
                if policy.prevention is not None
            )
        if (
            self.mode
            in {
                GuardianMode.APPLY_OWNED_TRANSLATIONS,
                GuardianMode.PROPOSE_PREVENTION,
            }
            and self.limits.max_remediation_drafts_per_run > 0
        ):
            actors.extend(
                policy.closed_pr_backfill.remediation.publication_actor
                for policy in self.repositories
                if policy.closed_pr_backfill is not None
                and policy.closed_pr_backfill.remediation is not None
            )
        return tuple(actors)

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
        evidence = (
            (self.evidence,) if isinstance(self.evidence, str) else tuple(self.evidence)
        )
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
        if not 1 <= len(self.evidence_feedback_ids) <= _MAX_RECURRENCE_EVIDENCE_IDS:
            raise ValueError(
                "evidence_feedback_ids must contain between 1 and 100 items."
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
        if len(self.recurrence_candidates) > _MAX_RECURRENCE_CANDIDATES:
            raise ValueError("recurrence_candidates must contain at most 100 items.")
