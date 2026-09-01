"""Strict YAML configuration for the localization PR guardian."""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator
import yaml

from localize.guardian.models import (
    AllowedHeadRepository,
    CodexAuthMode,
    ExactRepository,
    GuardianConfig,
    GuardianLimits,
    GuardianMode,
    GuardianRuntime,
    PreventionPolicy,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.signing import canonical_signing_key


class GuardianConfigError(ValueError):
    """Raised when guardian configuration is malformed or unsafe."""


_NON_EMPTY_STRING = {"type": "string", "minLength": 1}
_NON_EMPTY_UNIQUE_STRINGS = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "items": _NON_EMPTY_STRING,
}
_REPOSITORY_NAME_PATTERN = (
    r"^(?!\.{1,2}/)(?![A-Za-z0-9_.-]+/\.{1,2}$)"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)


def _exact_repository_schema(*, ref_field: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["full_name", "id", ref_field],
        "properties": {
            "full_name": {
                "type": "string",
                "pattern": _REPOSITORY_NAME_PATTERN,
            },
            "id": {"type": "integer", "minimum": 1},
            ref_field: _NON_EMPTY_STRING,
        },
    }


_ARGV_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 64,
    "items": _NON_EMPTY_STRING,
}

_PREVENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "target_repository",
        "push_repository",
        "allowed_code_path_globs",
        "allowed_test_path_globs",
        "focused_test_argv",
        "sandbox_argv_prefix",
        "max_changed_files",
        "max_changed_bytes",
        "private_target_model_opt_in",
    ],
    "properties": {
        "target_repository": _exact_repository_schema(ref_field="base_branch"),
        "push_repository": _exact_repository_schema(ref_field="branch_prefix"),
        "allowed_code_path_globs": _NON_EMPTY_UNIQUE_STRINGS,
        "allowed_test_path_globs": _NON_EMPTY_UNIQUE_STRINGS,
        "focused_test_argv": {
            "type": "array",
            "minItems": 1,
            "items": _ARGV_SCHEMA,
        },
        "sandbox_argv_prefix": _ARGV_SCHEMA,
        "max_changed_files": {"type": "integer", "minimum": 1},
        "max_changed_bytes": {"type": "integer", "minimum": 1},
        "private_target_model_opt_in": {"type": "boolean"},
    },
}


def _actor_list_schema(actor_types: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "uniqueItems": True,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["login", "id", "type"],
            "properties": {
                "login": _NON_EMPTY_STRING,
                "id": {"type": "integer", "minimum": 1},
                "type": {"enum": list(actor_types)},
            },
        },
    }

_CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["repositories"],
    "properties": {
        "mode": {"enum": [mode.value for mode in GuardianMode]},
        "runtime": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "codex_model": _NON_EMPTY_STRING,
                "codex_auth_mode": {
                    "enum": [mode.value for mode in CodexAuthMode]
                },
                "codex_home": _NON_EMPTY_STRING,
                "codex_reasoning_effort": {
                    "enum": ["low", "medium", "high", "xhigh", "max", "ultra"]
                },
                "codex_executable": _NON_EMPTY_STRING,
                "git_executable": _NON_EMPTY_STRING,
                "signing_program": _NON_EMPTY_STRING,
                "github_token_command": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": _NON_EMPTY_STRING,
                },
                "codex_api_key_command": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": _NON_EMPTY_STRING,
                },
                "signing_key": _NON_EMPTY_STRING,
            },
        },
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_timeout_seconds": {"type": "integer", "minimum": 1},
                "max_attempts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2,
                },
                "max_value_edits_per_run": {"type": "integer", "minimum": 0},
                "max_prevention_drafts_per_run": {
                    "type": "integer",
                    "minimum": 0,
                },
                "max_model_calls_per_day": {
                    "type": "integer",
                    "minimum": 1,
                },
                "daily_cost_limit_usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "model_call_reservation_usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "min_apply_confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "raw_retention_days": {"type": "integer", "minimum": 1},
            },
        },
        "repositories": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "base_repo",
                    "base_repo_id",
                    "base_branch",
                    "allowed_pr_authors",
                    "allowed_head_owners",
                    "allowed_head_repositories",
                    "allowed_branch_globs",
                    "allowed_path_globs",
                    "pipeline_config_path",
                    "source_locale",
                    "trusted_reviewers",
                    "trusted_bots",
                ],
                "properties": {
                    "base_repo": {
                        "type": "string",
                        "pattern": _REPOSITORY_NAME_PATTERN,
                    },
                    "base_repo_id": {"type": "integer", "minimum": 1},
                    "base_branch": _NON_EMPTY_STRING,
                    "private_repo_model_opt_in": {"type": "boolean"},
                    "allowed_pr_authors": _actor_list_schema(("User", "Bot")),
                    "allowed_head_owners": _actor_list_schema(
                        ("User", "Bot", "Organization")
                    ),
                    "allowed_head_repositories": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["full_name", "id"],
                            "properties": {
                                "full_name": {
                                    "type": "string",
                                    "pattern": _REPOSITORY_NAME_PATTERN,
                                },
                                "id": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                    "allowed_branch_globs": _NON_EMPTY_UNIQUE_STRINGS,
                    "allowed_path_globs": _NON_EMPTY_UNIQUE_STRINGS,
                    "pipeline_config_path": _NON_EMPTY_STRING,
                    "source_locale": {
                        "type": "string",
                        "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
                    },
                    "trusted_reviewers": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": _actor_list_schema(("User",)),
                        "propertyNames": {
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
                        },
                    },
                    "trusted_bots": {
                        "type": "object",
                        "additionalProperties": _actor_list_schema(("Bot",)),
                        "propertyNames": {
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
                        },
                    },
                    "prevention": _PREVENTION_SCHEMA,
                },
            },
        },
    },
}


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _format_validation_error(error: Any) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    if path:
        return f"Invalid guardian configuration at {path}: {error.message}"
    return f"Invalid guardian configuration: {error.message}"


def _validate_relative_path(value: str, *, field: str) -> None:
    if "\\" in value or "\x00" in value:
        raise GuardianConfigError(f"{field} must be a relative POSIX path or glob.")
    path = PurePosixPath(value)
    if path.is_absolute() or value in {"", "."} or ".." in path.parts:
        raise GuardianConfigError(f"{field} must be a relative POSIX path or glob.")


_SAFE_PATH_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*?@+\-]+$")
_SAFE_BRANCH_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*?@+\-]+$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")
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


def _validate_path_glob(value: str, *, field: str) -> None:
    _validate_single_line(value, field=field)
    _validate_relative_path(value, field=field)
    path = PurePosixPath(value)
    if (
        not _SAFE_PATH_GLOB_RE.fullmatch(value)
        or "//" in value
        or value.startswith("-")
        or ".git" in path.parts
    ):
        raise GuardianConfigError(f"{field} must be a safe relative POSIX path glob.")


def _validate_plain_relative_path(value: str, *, field: str) -> None:
    _validate_single_line(value, field=field)
    _validate_relative_path(value, field=field)
    if (
        any(character in value for character in "*?[]")
        or any(part.casefold() == ".git" for part in PurePosixPath(value).parts)
    ):
        raise GuardianConfigError(f"{field} must be one exact relative POSIX path.")


def _validate_branch_glob(value: str, *, field: str) -> None:
    _validate_single_line(value, field=field)
    candidate = value.replace("*", "x").replace("?", "x")
    if (
        not _SAFE_BRANCH_GLOB_RE.fullmatch(value)
        or value.startswith(("-", "refs/"))
    ):
        raise GuardianConfigError(f"{field} must be a safe branch-name glob.")
    _validate_branch_scope(candidate, field=field, prefix=False)


def _validate_branch_scope(value: str, *, field: str, prefix: bool) -> None:
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
        raise GuardianConfigError(f"{field} must be a safe Git {kind}.")


def _executable_name(value: str) -> str:
    return PurePosixPath(value).name.casefold()


def _validate_argv(
    raw_argv: list[str],
    *,
    field: str,
    absolute_executable: bool | None,
) -> tuple[str, ...]:
    argv = tuple(raw_argv)
    for index, argument in enumerate(argv):
        _validate_single_line(argument, field=f"{field}.{index}")

    executable_path = PurePosixPath(argv[0])
    if absolute_executable is True:
        if not executable_path.is_absolute() or ".." in executable_path.parts:
            raise GuardianConfigError(
                f"{field}.0 must be an absolute POSIX executable path."
            )
    elif absolute_executable is False and (
        executable_path.is_absolute() or ".." in executable_path.parts
    ):
        raise GuardianConfigError(
            f"{field}.0 must be a bare or repository-relative executable."
        )
    elif ".." in executable_path.parts:
        raise GuardianConfigError(
            f"{field}.0 must not traverse parent directories."
        )

    executable = _executable_name(argv[0])
    if executable in _SHELL_EXECUTABLES:
        raise GuardianConfigError(f"{field} must not invoke a shell wrapper.")
    for index, argument in enumerate(argv):
        if _ENV_ASSIGNMENT_RE.match(argument) or _CREDENTIAL_OPTION_RE.match(argument):
            raise GuardianConfigError(
                f"{field}.{index} must not contain credentials or environment assignments."
            )
    if executable.startswith("python") and "-c" in argv[1:]:
        raise GuardianConfigError(
            f"{field} must not contain an interpreter command string."
        )
    if executable in {"node", "perl", "ruby"} and any(
        argument in {"-e", "--eval"} for argument in argv[1:]
    ):
        raise GuardianConfigError(
            f"{field} must not contain an interpreter command string."
        )
    if executable == "php" and "-r" in argv[1:]:
        raise GuardianConfigError(
            f"{field} must not contain an interpreter command string."
        )
    return argv


def _validate_single_line(value: str, *, field: str) -> None:
    if (
        not value
        or len(value) > 4096
        or not value.isprintable()
    ):
        raise GuardianConfigError(f"{field} must be a non-empty single-line value.")


def _runtime_command(
    raw_runtime: Mapping[str, Any],
    field: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    raw_command = raw_runtime.get(field)
    if raw_command is None:
        return default
    return _validate_argv(
        raw_command,
        field=f"runtime.{field}",
        absolute_executable=None,
    )


def _validate_codex_home(value: str) -> None:
    """Accept one absolute or home-relative private credential directory."""

    _validate_single_line(value, field="runtime.codex_home")
    if "\\" in value or "\x00" in value or any(char in value for char in "*?[]"):
        raise GuardianConfigError(
            "runtime.codex_home must be an absolute or ~/ POSIX directory."
        )
    candidate = value[2:] if value.startswith("~/") else value
    path = PurePosixPath(candidate)
    if (
        not candidate
        or ".." in path.parts
        or (not value.startswith("~/") and not path.is_absolute())
    ):
        raise GuardianConfigError(
            "runtime.codex_home must be an absolute or ~/ POSIX directory."
        )


def _parse_prevention_policy(
    raw_policy: Mapping[str, Any],
    *,
    repository_index: int,
) -> PreventionPolicy | None:
    raw_prevention = raw_policy.get("prevention")
    if raw_prevention is None:
        return None

    field = f"repositories.{repository_index}.prevention"
    target = raw_prevention["target_repository"]
    push = raw_prevention["push_repository"]
    _validate_single_line(
        target["full_name"],
        field=f"{field}.target_repository.full_name",
    )
    _validate_single_line(
        push["full_name"],
        field=f"{field}.push_repository.full_name",
    )
    _validate_branch_scope(
        target["base_branch"],
        field=f"{field}.target_repository.base_branch",
        prefix=False,
    )
    _validate_branch_scope(
        push["branch_prefix"],
        field=f"{field}.push_repository.branch_prefix",
        prefix=True,
    )

    code_globs = tuple(raw_prevention["allowed_code_path_globs"])
    test_globs = tuple(raw_prevention["allowed_test_path_globs"])
    for category, path_globs in (
        ("allowed_code_path_globs", code_globs),
        ("allowed_test_path_globs", test_globs),
    ):
        for path_index, path_glob in enumerate(path_globs):
            _validate_path_glob(
                path_glob,
                field=f"{field}.{category}.{path_index}",
            )
    overlap = set(code_globs) & set(test_globs)
    if overlap:
        raise GuardianConfigError(
            f"{field} code and test path glob allowlists must not overlap."
        )

    focused_test_argv = tuple(
        _validate_argv(
            raw_argv,
            field=f"{field}.focused_test_argv.{argv_index}",
            absolute_executable=True,
        )
        for argv_index, raw_argv in enumerate(raw_prevention["focused_test_argv"])
    )
    if len(focused_test_argv) != len(set(focused_test_argv)):
        raise GuardianConfigError(f"{field} has duplicate focused test argv.")
    sandbox_argv_prefix = _validate_argv(
        raw_prevention["sandbox_argv_prefix"],
        field=f"{field}.sandbox_argv_prefix",
        absolute_executable=True,
    )

    target_name = target["full_name"].casefold()
    push_name = push["full_name"].casefold()
    same_name = target_name == push_name
    same_id = target["id"] == push["id"]
    if same_name != same_id:
        raise GuardianConfigError(
            f"{field} has an ambiguous repository identity: a numeric ID and full "
            "name must identify the same repository."
        )

    return PreventionPolicy(
        target_repository=ExactRepository(
            full_name=target["full_name"],
            id=target["id"],
        ),
        target_base_branch=target["base_branch"],
        push_repository=ExactRepository(
            full_name=push["full_name"],
            id=push["id"],
        ),
        push_branch_prefix=push["branch_prefix"],
        allowed_code_path_globs=code_globs,
        allowed_test_path_globs=test_globs,
        focused_test_argv=focused_test_argv,
        sandbox_argv_prefix=sandbox_argv_prefix,
        max_changed_files=raw_prevention["max_changed_files"],
        max_changed_bytes=raw_prevention["max_changed_bytes"],
        private_target_model_opt_in=raw_prevention[
            "private_target_model_opt_in"
        ],
    )


def parse_guardian_config(raw_config: Mapping[str, Any]) -> GuardianConfig:
    """Validate an already-loaded mapping and return immutable typed policy."""

    raw_limits_for_finite_check = raw_config.get("limits")
    if isinstance(raw_limits_for_finite_check, Mapping):
        for finite_key in (
            "daily_cost_limit_usd",
            "model_call_reservation_usd",
            "min_apply_confidence",
        ):
            raw_number = raw_limits_for_finite_check.get(finite_key)
            if isinstance(raw_number, (int, float)) and not isinstance(
                raw_number,
                bool,
            ) and not math.isfinite(raw_number):
                raise GuardianConfigError(f"limits.{finite_key} must be a finite number.")

    errors = sorted(
        Draft202012Validator(_CONFIG_SCHEMA).iter_errors(raw_config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise GuardianConfigError(_format_validation_error(errors[0]))

    mode = GuardianMode(raw_config.get("mode", GuardianMode.OBSERVE.value))
    raw_repositories = raw_config["repositories"]
    repositories: list[RepositoryPolicy] = []
    seen_repositories: set[str] = set()
    for index, raw_policy in enumerate(raw_repositories):
        base_repo = raw_policy["base_repo"]
        _validate_single_line(base_repo, field=f"repositories.{index}.base_repo")
        _validate_branch_scope(
            raw_policy["base_branch"],
            field=f"repositories.{index}.base_branch",
            prefix=False,
        )
        normalized_repo = base_repo.casefold()
        if normalized_repo in seen_repositories:
            raise GuardianConfigError(
                f"repositories.{index}.base_repo duplicates {base_repo!r}."
            )
        seen_repositories.add(normalized_repo)

        _validate_plain_relative_path(
            raw_policy["pipeline_config_path"],
            field=f"repositories.{index}.pipeline_config_path",
        )
        _validate_single_line(
            raw_policy["source_locale"],
            field=f"repositories.{index}.source_locale",
        )
        for branch_index, branch_glob in enumerate(
            raw_policy["allowed_branch_globs"]
        ):
            _validate_branch_glob(
                branch_glob,
                field=f"repositories.{index}.allowed_branch_globs.{branch_index}",
            )
        for path_index, path_glob in enumerate(raw_policy["allowed_path_globs"]):
            _validate_path_glob(
                path_glob,
                field=f"repositories.{index}.allowed_path_globs.{path_index}",
            )

        for category in ("allowed_pr_authors", "allowed_head_owners"):
            for actor_index, actor in enumerate(raw_policy[category]):
                _validate_single_line(
                    actor["login"],
                    field=f"repositories.{index}.{category}.{actor_index}.login",
                )
        for repository_index, repository in enumerate(
            raw_policy["allowed_head_repositories"]
        ):
            _validate_single_line(
                repository["full_name"],
                field=(
                    f"repositories.{index}.allowed_head_repositories."
                    f"{repository_index}.full_name"
                ),
            )

        for locale in set(raw_policy["trusted_reviewers"]) | set(
            raw_policy["trusted_bots"]
        ):
            _validate_single_line(
                locale,
                field=f"repositories.{index}.trusted_locale",
            )
            seen_actor_ids: set[int] = set()
            for category in ("trusted_reviewers", "trusted_bots"):
                for actor_index, actor in enumerate(
                    raw_policy[category].get(locale, ())
                ):
                    _validate_single_line(
                        actor["login"],
                        field=(
                            f"repositories.{index}.{category}.{locale}."
                            f"{actor_index}.login"
                        ),
                    )
                    actor_id = actor["id"]
                    if actor_id in seen_actor_ids:
                        raise GuardianConfigError(
                            f"repositories.{index}.{locale} has duplicate actor id "
                            f"{actor_id}."
                        )
                    seen_actor_ids.add(actor_id)

        for category in ("allowed_pr_authors", "allowed_head_owners"):
            actor_ids = [actor["id"] for actor in raw_policy[category]]
            if len(actor_ids) != len(set(actor_ids)):
                raise GuardianConfigError(
                    f"repositories.{index}.{category} has a duplicate actor id."
                )

        head_repository_ids = [
            repository["id"] for repository in raw_policy["allowed_head_repositories"]
        ]
        head_repository_names = [
            repository["full_name"].casefold()
            for repository in raw_policy["allowed_head_repositories"]
        ]
        if len(head_repository_ids) != len(set(head_repository_ids)) or len(
            head_repository_names
        ) != len(set(head_repository_names)):
            raise GuardianConfigError(
                f"repositories.{index}.allowed_head_repositories has a duplicate "
                "repository identity."
            )

        prevention = _parse_prevention_policy(
            raw_policy,
            repository_index=index,
        )
        if mode is GuardianMode.PROPOSE_PREVENTION and prevention is None:
            raise GuardianConfigError(
                f"repositories.{index}.prevention is required in "
                "propose-prevention mode."
            )

        def actor_tuple(category: str) -> tuple[TrustedActor, ...]:
            return tuple(
                TrustedActor(
                    login=actor["login"],
                    id=actor["id"],
                    type=actor["type"],
                )
                for actor in raw_policy[category]
            )

        def actors_for(category: str) -> dict[str, tuple[TrustedActor, ...]]:
            expected_type = "User" if category == "trusted_reviewers" else "Bot"
            return {
                locale: tuple(
                    TrustedActor(
                        login=actor["login"],
                        id=actor["id"],
                        type=expected_type,
                    )
                    for actor in actors
                )
                for locale, actors in raw_policy[category].items()
            }

        repositories.append(
            RepositoryPolicy(
                base_repo=base_repo,
                base_repo_id=raw_policy["base_repo_id"],
                base_branch=raw_policy["base_branch"],
                allowed_pr_authors=actor_tuple("allowed_pr_authors"),
                allowed_head_owners=actor_tuple("allowed_head_owners"),
                allowed_head_repositories=tuple(
                    AllowedHeadRepository(
                        full_name=repository["full_name"],
                        id=repository["id"],
                    )
                    for repository in raw_policy["allowed_head_repositories"]
                ),
                allowed_branch_globs=tuple(raw_policy["allowed_branch_globs"]),
                allowed_path_globs=tuple(raw_policy["allowed_path_globs"]),
                pipeline_config_path=raw_policy["pipeline_config_path"],
                source_locale=raw_policy["source_locale"],
                trusted_reviewers=actors_for("trusted_reviewers"),
                trusted_bots=actors_for("trusted_bots"),
                private_repo_model_opt_in=raw_policy.get(
                    "private_repo_model_opt_in",
                    False,
                ),
                prevention=prevention,
            )
        )

    raw_runtime = raw_config.get("runtime", {})
    runtime_defaults = GuardianRuntime()
    auth_mode = CodexAuthMode(
        raw_runtime.get("codex_auth_mode", runtime_defaults.codex_auth_mode.value)
    )
    for field in (
        "codex_model",
        "codex_executable",
        "git_executable",
        "signing_program",
        "signing_key",
    ):
        value = raw_runtime.get(field)
        if value is not None:
            _validate_single_line(value, field=f"runtime.{field}")
    codex_home = raw_runtime.get("codex_home", runtime_defaults.codex_home)
    _validate_codex_home(codex_home)
    raw_signing_key = raw_runtime.get("signing_key")
    if raw_signing_key is not None:
        try:
            raw_signing_key = canonical_signing_key(raw_signing_key)
        except ValueError as exc:
            raise GuardianConfigError(f"runtime.signing_key {exc}") from None
    codex_api_key_command = _runtime_command(
        raw_runtime,
        "codex_api_key_command",
        runtime_defaults.codex_api_key_command,
    )
    if auth_mode is CodexAuthMode.CHATGPT and codex_api_key_command:
        raise GuardianConfigError(
            "runtime.codex_api_key_command is only valid with "
            "runtime.codex_auth_mode: api-key."
        )
    if auth_mode is CodexAuthMode.API_KEY and not codex_api_key_command:
        raise GuardianConfigError(
            "runtime.codex_api_key_command is required with "
            "runtime.codex_auth_mode: api-key."
        )
    if auth_mode is CodexAuthMode.API_KEY and "codex_home" in raw_runtime:
        raise GuardianConfigError(
            "runtime.codex_home is only valid with "
            "runtime.codex_auth_mode: chatgpt."
        )

    runtime = GuardianRuntime(
        codex_model=raw_runtime.get("codex_model", runtime_defaults.codex_model),
        codex_reasoning_effort=raw_runtime.get(
            "codex_reasoning_effort",
            runtime_defaults.codex_reasoning_effort,
        ),
        codex_auth_mode=auth_mode,
        codex_home=codex_home,
        codex_executable=raw_runtime.get(
            "codex_executable",
            runtime_defaults.codex_executable,
        ),
        git_executable=raw_runtime.get(
            "git_executable",
            runtime_defaults.git_executable,
        ),
        signing_program=raw_runtime.get(
            "signing_program",
            runtime_defaults.signing_program,
        ),
        github_token_command=_runtime_command(
            raw_runtime,
            "github_token_command",
            runtime_defaults.github_token_command,
        ),
        codex_api_key_command=codex_api_key_command,
        signing_key=raw_signing_key,
    )

    raw_limits = raw_config.get("limits", {})
    defaults = GuardianLimits()
    has_daily_cost = "daily_cost_limit_usd" in raw_limits
    has_reservation = "model_call_reservation_usd" in raw_limits
    if auth_mode is CodexAuthMode.CHATGPT and (has_daily_cost or has_reservation):
        offending = (
            "daily_cost_limit_usd" if has_daily_cost else "model_call_reservation_usd"
        )
        raise GuardianConfigError(
            f"limits.{offending} is only valid with runtime.codex_auth_mode: api-key."
        )
    if auth_mode is CodexAuthMode.API_KEY and not has_daily_cost:
        raise GuardianConfigError(
            "limits.daily_cost_limit_usd is required with "
            "runtime.codex_auth_mode: api-key."
        )
    if auth_mode is CodexAuthMode.API_KEY and not has_reservation:
        raise GuardianConfigError(
            "limits.model_call_reservation_usd is required with "
            "runtime.codex_auth_mode: api-key."
        )

    limits = GuardianLimits(
        run_timeout_seconds=raw_limits.get(
            "run_timeout_seconds",
            defaults.run_timeout_seconds,
        ),
        max_attempts=raw_limits.get("max_attempts", defaults.max_attempts),
        max_value_edits_per_run=raw_limits.get(
            "max_value_edits_per_run",
            defaults.max_value_edits_per_run,
        ),
        max_prevention_drafts_per_run=raw_limits.get(
            "max_prevention_drafts_per_run",
            defaults.max_prevention_drafts_per_run,
        ),
        max_model_calls_per_day=raw_limits.get(
            "max_model_calls_per_day",
            defaults.max_model_calls_per_day,
        ),
        daily_cost_limit_usd=(
            float(raw_limits["daily_cost_limit_usd"])
            if has_daily_cost
            else None
        ),
        model_call_reservation_usd=(
            float(raw_limits["model_call_reservation_usd"])
            if has_reservation
            else None
        ),
        min_apply_confidence=float(
            raw_limits.get("min_apply_confidence", defaults.min_apply_confidence)
        ),
        raw_retention_days=raw_limits.get(
            "raw_retention_days",
            defaults.raw_retention_days,
        ),
    )
    required_calls = limits.max_attempts * (
        1
        + (
            limits.max_prevention_drafts_per_run
            if mode is GuardianMode.PROPOSE_PREVENTION
            else 0
        )
    )
    if limits.max_model_calls_per_day < required_calls:
        raise GuardianConfigError(
            "limits.max_model_calls_per_day must provide retry capacity for one "
            "assessment and the configured prevention draft allowance."
        )
    if (
        limits.model_call_reservation_usd is not None
        and limits.daily_cost_limit_usd is not None
        and limits.model_call_reservation_usd > limits.daily_cost_limit_usd
    ):
        raise GuardianConfigError(
            "limits.model_call_reservation_usd must not exceed daily_cost_limit_usd."
        )
    if (
        mode is GuardianMode.PROPOSE_PREVENTION
        and limits.max_prevention_drafts_per_run > 0
        and limits.daily_cost_limit_usd is not None
        and limits.model_call_reservation_usd is not None
        and limits.daily_cost_limit_usd < limits.model_call_reservation_usd
        * limits.max_attempts
        * (1 + limits.max_prevention_drafts_per_run)
    ):
        raise GuardianConfigError(
            "propose-prevention requires daily_cost_limit_usd to reserve one "
            "assessment plus every configured prevention authoring call for "
            "every allowed attempt."
        )
    return GuardianConfig(
        repositories=tuple(repositories),
        mode=mode,
        limits=limits,
        runtime=runtime,
    )


def parse_guardian_config_yaml(raw_text: str) -> GuardianConfig:
    """Parse secret-free Guardian YAML already read from a trusted source."""

    if not isinstance(raw_text, str):
        raise GuardianConfigError("Guardian configuration must be UTF-8 text.")
    try:
        raw_config = yaml.load(raw_text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise GuardianConfigError("Unable to parse guardian configuration.") from exc

    if not isinstance(raw_config, Mapping):
        raise GuardianConfigError("Guardian configuration must be a YAML mapping.")
    return parse_guardian_config(raw_config)


def load_guardian_config(path: str | Path) -> GuardianConfig:
    """Load a secret-free guardian YAML file with strict schema validation."""

    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardianConfigError(
            f"Unable to load guardian configuration {config_path}: {exc}"
        ) from exc
    return parse_guardian_config_yaml(raw_text)
