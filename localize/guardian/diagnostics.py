"""Bounded, allowlisted failure evidence in the existing private health ledger.

Never persist raw stderr, exception text, argv, URLs, environments, or frame
locals. Remote output supplies only known reason codes, not diagnostic prose.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
import sys
from uuid import uuid4


@dataclass(frozen=True)
class AdapterFailure:
    stage: str
    operation: str
    reason: str
    exit_code: int | None = None
    http_status: int | None = None


_GIT_STAGES = {
    "commit": "sign",
    "verify-commit": "verify-signature",
    "push": "push",
    "ls-remote": "remote-read",
    "fetch": "fetch",
}
_GIT_REASONS = {
    "permission denied": "permission_denied",
    "authentication failed": "authentication_failed",
    "could not resolve": "dns_failed",
    "couldn't connect": "connection_failed",
    "failed to connect": "connection_failed",
    "failed to sign": "signing_failed",
    "no principal matched": "signer_mismatch",
    "incorrect signature": "signature_invalid",
    "bad signature": "signature_invalid",
    "no space left": "disk_full",
    "index.lock": "git_lock",
    "non-fast-forward": "non_fast_forward",
    "stale info": "lease_rejected",
    "protected branch": "protected_branch",
    "repository not found": "repository_not_found",
    "timed out": "timeout",
}
_INVARIANT_REASONS = {
    "exact SSH signing material is unavailable": "signing_material_unavailable",
    "SSH signing program is unavailable": "signing_program_unavailable",
    "SSH_AUTH_SOCK is only permitted for SSH git commit": "signing_environment_scope",
    "command environment cannot override Git security controls": "git_environment_override",
    "credential environment provider failed": "credential_helper_failed",
    "verified SSH commit signer does not match the configured fingerprint": "signer_mismatch",
    "verified commit signer does not match the configured fingerprint": "signer_mismatch",
    "working tree is not clean before remediation publication": "dirty_workspace",
    "working tree is not clean before prevention publication": "dirty_workspace",
    "remediation commit is not a direct child of exact base": "base_mismatch",
    "checkout HEAD no longer matches remediation commit": "head_mismatch",
}


def git_failure(error, *, operation, returncode=None, output="", reason=None):
    """Attach only fixed classifications before Git output is discarded."""
    operation = operation if operation in _GIT_STAGES else "git"
    excerpt = output[:16384].lower()
    classified = next(
        (code for text, code in _GIT_REASONS.items() if text in excerpt), "git_failed"
    )
    error.guardian_failure = AdapterFailure(
        _GIT_STAGES.get(operation, "workspace"),
        operation,
        reason or classified,
        exit_code=returncode,
    )
    return error


def http_failure(error, *, method, status, create_pr=False, reason="http_error"):
    error.guardian_failure = AdapterFailure(
        "create-pr" if create_pr else "github-api",
        method if method in {"GET", "POST", "PATCH", "PUT", "DELETE"} else "HTTP",
        reason,
        http_status=status,
    )
    return error


@dataclass
class _Audit:
    state: object
    poll_id: str = field(default_factory=lambda: str(uuid4()))
    count: int = 0
    last_id: int | None = None
    seen: list[BaseException] = field(default_factory=list)


_AUDIT: ContextVar[_Audit | None] = ContextVar("guardian_failure_audit", default=None)


def record_failure(error, **context):
    audit = _AUDIT.get()
    if (
        audit is None
        or audit.count >= 32
        or any(error is prior for prior in audit.seen)
    ):
        return
    if type(error).__name__ == "GuardianRuntimeError" and audit.count:
        return  # Retain the already-recorded underlying error, not its public wrapper.
    locations = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "")
        if isinstance(module, str) and re.fullmatch(
            r"localize\.guardian\.[a-z_]+", module
        ):
            locations.append(f"{module}:{frame.f_code.co_name}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    name = type(error).__name__
    stage = "controller"
    for location in locations:
        if (
            ":publish_remediation_branch:" in location
            or ":publish_prevention_branch:" in location
        ):
            stage = "publish-branch"
        elif ":_verify_commit_signature:" in location:
            stage = "verify-signature"
        elif ":open_draft:" in location:
            stage = "create-pr"
    details = {
        "poll_id": audit.poll_id,
        "exception": name if re.fullmatch(r"[A-Za-z_]{1,80}", name) else "Exception",
        "stage": stage,
        "reason": "unclassified",
        "locations": locations[-8:],
    }
    adapter = getattr(error, "guardian_failure", None)
    if isinstance(adapter, AdapterFailure):
        details.update(
            stage=adapter.stage, operation=adapter.operation, reason=adapter.reason
        )
        if adapter.exit_code is not None:
            details["exit_code"] = adapter.exit_code
        if adapter.http_status is not None:
            details["http_status"] = adapter.http_status
    elif str(error) in _INVARIANT_REASONS:
        details.update(reason=_INVARIANT_REASONS[str(error)])
    cause = error.__cause__
    if isinstance(cause, OSError) and isinstance(cause.errno, int):
        details["os_errno"] = cause.errno
    for key, pattern in (
        ("repository", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
        ("run_id", r"[A-Za-z0-9-]+"),
    ):
        value = context.get(key)
        if (
            isinstance(value, str)
            and len(value) <= 200
            and re.fullmatch(pattern, value)
        ):
            details[key] = value
    numbers = context.get("pull_numbers")
    if isinstance(numbers, (list, tuple)):
        details["pull_numbers"] = [n for n in numbers[:30] if type(n) is int and n > 0]
    audit.last_id = audit.state.record_health(
        component="guardian-failure",
        status="failed",
        message="Private Guardian failure diagnostic.",
        details=details,
    )
    audit.count += 1
    audit.seen.append(error)


def announce_failure():
    audit = _AUDIT.get()
    reference = (
        f" #{audit.last_id}"
        if audit and audit.last_id
        else " (no detailed record available)"
    )
    print(
        f"Guardian poll failed; private diagnostic{reference}. Run guardian status for details.",
        file=sys.stderr,
    )


@contextmanager
def failure_audit(state):
    token = _AUDIT.set(_Audit(state))
    try:
        yield
    except Exception as error:
        record_failure(error)
        announce_failure()
        raise
    finally:
        _AUDIT.reset(token)
