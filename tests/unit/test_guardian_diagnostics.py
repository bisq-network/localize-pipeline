"""Private failure diagnostics retain bounded, redacted cause classifications."""

import pytest

from localize.guardian.diagnostics import failure_audit, http_failure, record_failure
from localize.guardian.github import GitHubAPIError
from localize.guardian.prevention_runtime import PreventionSourceAuthorityError


class _HealthState:
    def __init__(self):
        self.records = []

    def record_health(self, **kwargs):
        self.records.append(kwargs)
        return len(self.records)


def _wrapped_failure(cause):
    try:
        raise cause
    except Exception as error:
        try:
            raise PreventionSourceAuthorityError(
                "The open prevention source is no longer authorized."
            ) from error
        except PreventionSourceAuthorityError as wrapper:
            return wrapper


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    [
        ("GitHub pull request changed during hydration", "hydration_changed"),
        (
            "GitHub pull-request feedback exceeded the intake bound",
            "intake_bounds",
        ),
        ("private URL https://secret.invalid/token", "provider_error"),
    ],
)
def test_wrapped_provider_failure_records_only_fixed_cause_metadata(
    message, expected_reason
):
    state = _HealthState()
    error = _wrapped_failure(GitHubAPIError(message))

    with failure_audit(state):
        record_failure(error)

    details = state.records[0]["details"]
    assert details["exception"] == "PreventionSourceAuthorityError"
    assert details["reason"] == "unclassified"
    assert details["causes"][0]["exception"] == "GitHubAPIError"
    assert details["causes"][0]["reason"] == expected_reason
    assert message not in str(details)
    assert "secret.invalid" not in str(details)


def test_wrapped_http_failure_retains_safe_adapter_status_and_reason():
    state = _HealthState()
    cause = http_failure(
        GitHubAPIError("private token in response"),
        method="GET",
        status=503,
    )

    with failure_audit(state):
        record_failure(_wrapped_failure(cause))

    details = state.records[0]["details"]
    assert details["causes"][0]["exception"] == "GitHubAPIError"
    assert details["causes"][0]["reason"] == "http_error"
    assert details["causes"][0]["http_status"] == 503
    assert "private token" not in str(details)


def test_unknown_cause_chain_is_bounded_redacted_and_cycle_safe():
    state = _HealthState()
    errors = [RuntimeError(f"secret-{i}") for i in range(7)]
    for first, second in zip(errors, errors[1:]):
        first.__cause__ = second
    errors[-1].__cause__ = errors[0]

    with failure_audit(state):
        record_failure(_wrapped_failure(errors[0]))

    details = state.records[0]["details"]
    assert len(details["causes"]) <= 3
    assert all(item == {"exception": "Exception", "reason": "unclassified"}
               for item in details["causes"])
    assert "secret-" not in str(details)
