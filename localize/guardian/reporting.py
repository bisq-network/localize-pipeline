"""Deterministic public explanations, separate from private model rationale.

Only enumerated explanations and validated repository identifiers reach GitHub.
Never interpolate model prose, file paths, configuration, or replacement values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

REASONS = {
    "processing_failure": "Processing did not complete. Work remains deferred under the configured retry, quota, and authority limits.",
    "unspecified": "The assessment did not establish a more specific public reason.",
    "as_suggested": "The assessment accepted the suggested correction; publication status is reported separately.",
    "alternative_glossary": "The suggested terminology conflicts with the configured glossary; a glossary-compliant alternative was selected.",
    "alternative_source_fidelity": "An alternative was selected to preserve the current source meaning.",
    "alternative_other": "A validated alternative was selected instead of the literal suggestion; reviewer confirmation is needed.",
    "glossary_conflict": "The suggested terminology conflicts with the configured glossary. No glossary change has been authorized.",
    "policy_conflict": "The suggested change conflicts with configured edit authority or validation requirements.",
    "insufficient_evidence": "The available evidence is insufficient to authorize a correction.",
    "already_addressed": "The reported issue is already addressed in the assessed revision.",
    "not_applicable": "The suggestion does not apply to the assessed revision.",
}


def report_disposition(details: Mapping[str, object]) -> str:
    """A correction, decision, and delivery status are independent facts."""
    outcome = details.get("report_outcome", details.get("outcome"))
    if outcome == "applied":
        reason = str(details.get("report_reason", "unspecified"))
        return "applied_alternative" if reason.startswith("alternative_") else "applied"
    if outcome in {"translation_batch_deferred", "failed", "prepared", "would_apply"}:
        return "deferred"
    if outcome in {"already_addressed", "not_applicable"}:
        return str(outcome)
    return (
        "needs_human"
        if details.get("decision_required") or outcome == "needs_human"
        else "not_applied"
    )


def report_body(
    details: Mapping[str, object],
    *,
    repository: str,
    feedback_id: str,
    pull_number: int | None = None,
    web_base_url: str = "https://github.com",
) -> str:
    """Render public text without copying any free-form assessment content."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid report repository")
    if not re.fullmatch(
        r"(?:review_comment|issue_comment|review):[1-9][0-9]*(?::[A-Za-z0-9_.-]+)*",
        feedback_id,
    ):
        raise ValueError("Invalid report feedback identifier")
    reason = details.get("report_reason", "unspecified")
    if reason not in REASONS:
        raise ValueError("Unsupported public explanation code")
    commit = details.get("commit_sha")
    if commit is not None and (
        not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
    ):
        raise ValueError("Invalid report commit")
    disposition = report_disposition(details)
    labels = {
        "applied": "Applied a validated correction",
        "applied_alternative": "Applied an alternative correction",
        "already_addressed": "Already addressed",
        "not_applicable": "Not applicable",
        "deferred": "Deferred work",
        "needs_human": "Maintainer decision needed",
        "not_applied": "No eligible correction",
    }
    kind, identifier = feedback_id.split(":", 1)
    number = identifier.split(":", 1)[0]
    anchor = {
        "review_comment": "discussion_r",
        "issue_comment": "issuecomment-",
        "review": "pullrequestreview-",
    }[kind]
    # Review-object links are generated from validated IDs, never supplied URLs.
    body = f"🤖 **Localize Guardian — {labels[disposition]}** (`{feedback_id}`).\n\n"
    if pull_number is not None:
        if type(pull_number) is not int or pull_number <= 0:
            raise ValueError("Invalid report pull number")
        body += f"[Source feedback]({web_base_url}/{repository}/pull/{pull_number}#{anchor}{number}). "
    if commit:
        body += f"Published correction: [`{commit[:12]}`]({web_base_url}/{repository}/commit/{commit}). "
    else:
        body += "No correction was published for this assessment. "
    body += REASONS[reason]
    if details.get("report_outcome") == "failed" and reason != "processing_failure":
        body += " " + REASONS["processing_failure"]
    if disposition == "deferred":
        body += " Remaining work is not being claimed as completed."
    if details.get("decision_required"):
        body += " An explicit maintainer decision is still required. The operator must update the governed configuration before automatic reconsideration; a bot acknowledgement or PR merge is not policy approval."
        remaining = (
            details.get("held_value_edits") or details.get("deferred_value_edits") or 0
        )
        if type(remaining) is not int or not 0 <= remaining <= 100000:
            raise ValueError("Invalid held edit count")
        if remaining:
            body += f" {remaining} remaining value edits are human-held, not claimed as completed or automatically retryable."
    return body + "\n\nThe review thread remains open for reviewer confirmation."


def report_key(
    *, repository_id: int, pull_number: int, feedback_id: str, body: str
) -> str:
    """Deduplicate a logical explanation across retries and assessment runs."""
    return hashlib.sha256(
        json.dumps(
            [repository_id, pull_number, feedback_id, body], ensure_ascii=False
        ).encode()
    ).hexdigest()


def summary_body(
    reports,
    *,
    repository: str,
    pull_number: int,
    web_base_url: str = "https://github.com",
) -> str:
    """One bounded, repository-bound summary without model-controlled prose."""
    lines = [
        "<!-- localize-guardian:feedback-summary:v1 -->",
        "🤖 **Localize Guardian — feedback status**",
        "",
        "Assessment results; not a replacement for CI or translation validation.",
        "",
    ]
    allowed = {
        "applied",
        "applied_alternative",
        "already_addressed",
        "not_applicable",
        "deferred",
        "needs_human",
        "not_applied",
    }
    if len(reports) > 100:
        raise ValueError("Feedback summary exceeds its publication bound.")
    if not reports:
        lines.append(
            "No current authorized feedback reports are available. Withdrawal of feedback does not approve a glossary change or settle a recorded maintainer decision."
        )
    for report in reports:
        url, disposition = report["url"], report["disposition"]
        prefix = f"{web_base_url}/{repository}/pull/{pull_number}#"
        if (
            not isinstance(url, str)
            or not url.startswith(prefix)
            or not re.fullmatch(
                r"(?:discussion_r|issuecomment-)[1-9][0-9]*", url[len(prefix) :]
            )
            or disposition not in allowed
        ):
            raise ValueError("Invalid feedback summary entry.")
        suffix = (
            " — maintainer decision still required"
            if report.get("decision_required")
            else ""
        )
        lines.append(f"- [{disposition.replace('_', ' ')}]({url}){suffix}")
    return "\n".join(lines)
