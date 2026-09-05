"""Defense-in-depth tests for prevention policy and planning bounds."""

from __future__ import annotations

from pathlib import Path

import pytest

from localize.guardian.models import (
    ExactRepository,
    GuardianAssessment,
    PreventionPolicy,
    RecurrenceCandidate,
    TrustedActor,
)
from localize.guardian.prevention import (
    PreventionPolicyError,
    TestCommandResult,
    TestOutcome,
    inspect_prevention_patch,
)


def _policy(**overrides: object) -> PreventionPolicy:
    values: dict[str, object] = {
        "target_repository": ExactRepository("acme/pipeline", 501),
        "target_base_branch": "main",
        "push_repository": ExactRepository("localize-bot/pipeline", 502),
        "push_branch_prefix": "guardian/prevention-",
        "publication_actor": TrustedActor("localize-machine", 11, "User"),
        "allowed_code_path_globs": ("localize/**/*.py",),
        "allowed_test_path_globs": ("tests/**/*.py",),
        "focused_test_argv": (("/opt/bin/pytest", "tests/unit/test_rules.py"),),
        "sandbox_argv_prefix": ("/usr/bin/guardian-sandbox-wrapper",),
        "max_changed_files": 4,
        "max_changed_bytes": 262_144,
    }
    values.update(overrides)
    return PreventionPolicy(**values)  # type: ignore[arg-type]


def test_direct_prevention_policy_accepts_exact_collection_and_byte_bounds() -> None:
    exact_utf8_string = "é" * 2048
    commands = tuple(
        (f"/opt/bin/check-{index}", *(["arg"] * 254), exact_utf8_string)
        for index in range(64)
    )

    policy = _policy(
        push_branch_prefix="g/" + "a" * 176,
        allowed_code_path_globs=tuple(
            f"localize/generated_{index}.py" for index in range(100)
        ),
        allowed_test_path_globs=tuple(
            f"tests/generated_{index}.py" for index in range(100)
        ),
        focused_test_argv=commands,
        max_changed_files=100,
    )

    assert len(policy.allowed_code_path_globs) == 100
    assert len(policy.allowed_test_path_globs) == 100
    assert len(policy.focused_test_argv) == 64
    assert len(policy.focused_test_argv[0]) == 256
    assert policy.sandbox_argv_prefix == ("/usr/bin/guardian-sandbox-wrapper",)
    assert len(policy.focused_test_argv[0][-1].encode("utf-8")) == 4096
    assert len(policy.push_branch_prefix) + 77 == 255
    assert policy.max_changed_files == 100


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "allowed_code_path_globs": tuple(
                    f"localize/generated_{index}.py" for index in range(101)
                )
            },
            "code path glob",
        ),
        (
            {
                "allowed_test_path_globs": tuple(
                    f"tests/generated_{index}.py" for index in range(101)
                )
            },
            "test path glob",
        ),
        (
            {
                "focused_test_argv": tuple(
                    (f"/opt/bin/check-{index}",) for index in range(65)
                )
            },
            "focused test command",
        ),
        (
            {"focused_test_argv": (("/opt/bin/pytest", *(["arg"] * 256)),)},
            "focused.*test.*argv",
        ),
        (
            {"sandbox_argv_prefix": ("/usr/bin/sandbox-exec", "--")},
            "exactly one direct wrapper",
        ),
        (
            {"focused_test_argv": (("/opt/bin/pytest", "é" * 2049),)},
            "4096 UTF-8 bytes",
        ),
        (
            {"allowed_code_path_globs": ("é" * 2049,)},
            "4096 UTF-8 bytes",
        ),
        ({"target_base_branch": "é" * 2049}, "4096 UTF-8 bytes"),
        ({"max_changed_files": 101}, "max_changed_files"),
        ({"push_branch_prefix": "g/" + "a" * 177}, "generated branch"),
    ],
)
def test_direct_prevention_policy_rejects_values_above_runtime_bounds(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _policy(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_code_path_globs": None},
        {"allowed_code_path_globs": {"localize/**/*.py"}},
        {"allowed_test_path_globs": None},
        {"focused_test_argv": ("/opt/bin/pytest",)},
        {"sandbox_argv_prefix": None},
    ],
)
def test_direct_prevention_policy_rejects_malformed_collections(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="collection|sequence"):
        _policy(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target_base_branch": "refs/heads/main"}, "safe Git branch"),
        ({"push_branch_prefix": "refs/heads/prevention-"}, "branch prefix"),
        ({"allowed_code_path_globs": ("../localize/**",)}, "relative POSIX"),
        ({"allowed_test_path_globs": (".git/**",)}, "relative POSIX"),
        (
            {"allowed_code_path_globs": ("localize/**", "localize/**")},
            "duplicates",
        ),
        (
            {
                "allowed_code_path_globs": ("shared/**",),
                "allowed_test_path_globs": ("shared/**",),
            },
            "must not overlap",
        ),
        ({"focused_test_argv": (("venv/bin/pytest",),)}, "absolute POSIX"),
        ({"focused_test_argv": (("/bin/sh", "-c", "pytest"),)}, "shell wrapper"),
        (
            {"focused_test_argv": (("/opt/bin/pytest", "TOKEN=secret"),)},
            "credentials or environment assignments",
        ),
        (
            {
                "focused_test_argv": (
                    ("/opt/bin/pytest", "tests/unit/test_rules.py"),
                    ("/opt/bin/pytest", "tests/unit/test_rules.py"),
                )
            },
            "duplicates",
        ),
        ({"sandbox_argv_prefix": ("sandbox-exec",)}, "absolute POSIX"),
        (
            {"sandbox_argv_prefix": ("/usr/bin/python3",)},
            "direct sandbox wrapper",
        ),
        (
            {
                "push_repository": ExactRepository("acme/pipeline", 502),
            },
            "ambiguous identity",
        ),
    ],
)
def test_direct_prevention_policy_enforces_parser_authority_rules(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _policy(**overrides)


def test_direct_prevention_policy_freezes_mutable_authority_inputs() -> None:
    code_globs = ["localize/**/*.py"]
    test_globs = ["tests/**/*.py"]
    focused_commands = [["/opt/bin/pytest", "tests/unit/test_rules.py"]]
    sandbox_argv = ["/usr/bin/guardian-sandbox-wrapper"]

    policy = _policy(
        allowed_code_path_globs=code_globs,
        allowed_test_path_globs=test_globs,
        focused_test_argv=focused_commands,
        sandbox_argv_prefix=sandbox_argv,
    )
    code_globs.clear()
    test_globs.clear()
    focused_commands[0].append("--unsafe")
    focused_commands.clear()
    sandbox_argv.clear()

    assert policy.allowed_code_path_globs == ("localize/**/*.py",)
    assert policy.allowed_test_path_globs == ("tests/**/*.py",)
    assert policy.focused_test_argv == (
        ("/opt/bin/pytest", "tests/unit/test_rules.py"),
    )
    assert policy.sandbox_argv_prefix == ("/usr/bin/guardian-sandbox-wrapper",)

def test_direct_publication_actor_login_has_the_same_utf8_byte_bound() -> None:
    with pytest.raises(ValueError, match="login"):
        TrustedActor("é" * 2049, 11, "Bot")


@pytest.mark.parametrize(
    "argv",
    [
        ("/opt/bin/pytest", *(["arg"] * 256)),
        ("/opt/bin/pytest", "界" * 1366),
    ],
)
def test_direct_test_result_rejects_unbounded_argv(argv: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="argv"):
        TestCommandResult(
            phase="base",
            outcome=TestOutcome.FAILED,
            argv=argv,
            commit_sha="a" * 40,
            parent_sha=None,
            returncode=1,
            test_overlay_hash="b" * 64,
        )


def test_direct_patch_inspection_rejects_policy_bounds_before_workspace_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(PreventionPolicyError, match="file limit"):
        inspect_prevention_patch(
            base_workspace=tmp_path / "missing-base",
            candidate_workspace=tmp_path / "missing-candidate",
            allowed_code_path_globs=("localize/**/*.py",),
            allowed_test_path_globs=("tests/**/*.py",),
            max_changed_files=101,
        )

    with pytest.raises(PreventionPolicyError, match="glob.*bound"):
        inspect_prevention_patch(
            base_workspace=tmp_path / "missing-base",
            candidate_workspace=tmp_path / "missing-candidate",
            allowed_code_path_globs=tuple(
                f"localize/generated_{index}.py" for index in range(101)
            ),
            allowed_test_path_globs=("tests/**/*.py",),
            max_changed_files=4,
        )

    with pytest.raises(PreventionPolicyError, match="4096 UTF-8 bytes"):
        inspect_prevention_patch(
            base_workspace=tmp_path / "missing-base",
            candidate_workspace=tmp_path / "missing-candidate",
            allowed_code_path_globs=("é" * 2049,),
            allowed_test_path_globs=("tests/**/*.py",),
            max_changed_files=4,
        )


def test_direct_recurrence_models_reject_worksets_above_schema_bounds() -> None:
    evidence_ids = tuple(f"review_comment:{index}" for index in range(101))
    with pytest.raises(ValueError, match="evidence_feedback_ids"):
        RecurrenceCandidate(
            scope="pipeline_code",
            summary="Bound model evidence",
            evidence_feedback_ids=evidence_ids,
        )

    candidate = RecurrenceCandidate(
        scope="pipeline_code",
        summary="Bound model evidence",
        evidence_feedback_ids=("review_comment:1",),
    )
    with pytest.raises(ValueError, match="recurrence_candidates"):
        GuardianAssessment(
            feedback_id="review_comment:1",
            verdict="needs_human",
            confidence=1.0,
            rationale="The bounded model must reject oversized direct construction.",
            recurrence_candidates=(candidate,) * 101,
        )
