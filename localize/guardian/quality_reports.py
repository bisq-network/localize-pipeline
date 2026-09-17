"""Bounded machine findings; a producer identity is never reviewer authority.

Only enum-valued deterministic findings are transported. The controller must
recompute them from its exact trusted-source/current-target evidence before a
model is invoked. Semantic/model/glossary narratives are deliberately absent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from localize.semantic_quality import normalize_value
from localize.translation_validator import find_disallowed_control_characters

MARKER = "<!-- localize-pipeline:quality:v1 -->\n"
MAX_FINDINGS = 100
MAX_REPORT_BYTES = 60_000
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_FIELDS = {"version", "repository", "repository_id", "pull_id", "pull_number",
           "head_repository_id", "head_sha", "base_sha", "path", "locale", "findings"}
_FINDING_FIELDS = {"key", "category", "source_sha256", "target_sha256"}


def value_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def finding(key: str, source: str, target: str, category: str) -> dict[str, str]:
    return {"key": key, "category": category, "source_sha256": value_digest(source),
            "target_sha256": value_digest(target)}


def deterministic_categories(key: str, source: str, target: str,
                             brands: Sequence[str] = ()) -> tuple[str, ...]:
    # Lazy import keeps the quality gate's producer dependency acyclic.
    from localize.translation_quality_gate import is_expected_source_identical

    result = []
    if (normalize_value(source) == normalize_value(target)
            and not is_expected_source_identical(key, source, brands)):
        result.append("source_echo")
    if find_disallowed_control_characters(target):
        result.append("control_character")
    return tuple(result)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate machine-report field")
        result[key] = value
    return result


def parse_report(body: str) -> dict[str, Any]:
    if not isinstance(body, str) or not body.startswith(MARKER) or len(body.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("Not a bounded machine report")
    report = json.loads(body[len(MARKER):], object_pairs_hook=_unique_object)
    if not isinstance(report, dict) or set(report) != _FIELDS or type(report["version"]) is not int or report["version"] != 1:
        raise ValueError("Invalid machine-report schema")
    for name in ("repository_id", "pull_id", "pull_number", "head_repository_id"):
        if type(report[name]) is not int or report[name] <= 0:
            raise ValueError("Invalid machine-report identity")
    for name in ("head_sha", "base_sha"):
        if not isinstance(report[name], str) or not _COMMIT.fullmatch(report[name]):
            raise ValueError("Invalid machine-report commit")
    if not isinstance(report["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", report["repository"]):
        raise ValueError("Invalid machine-report repository")
    path = report["path"]
    if (not isinstance(path, str) or not path or len(path) > 1024
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(c in path for c in "\\\r\n\x00")):
        raise ValueError("Invalid machine-report path")
    if not isinstance(report["locale"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", report["locale"]):
        raise ValueError("Invalid machine-report locale")
    findings = report["findings"]
    if not isinstance(findings, list) or not 1 <= len(findings) <= MAX_FINDINGS:
        raise ValueError("Unbounded machine-report findings")
    seen = set()
    for item in findings:
        if not isinstance(item, dict) or set(item) != _FINDING_FIELDS:
            raise ValueError("Invalid machine finding")
        if not isinstance(item["key"], str) or not item["key"] or len(item["key"].encode("utf-8")) > 2048:
            raise ValueError("Invalid machine finding key")
        if item["category"] not in {"source_echo", "control_character"}:
            raise ValueError("Unsupported machine finding")
        for name in ("source_sha256", "target_sha256"):
            if not isinstance(item[name], str) or not _SHA.fullmatch(item[name]):
                raise ValueError("Invalid machine finding digest")
        identity = (item["key"], item["category"])
        if identity in seen:
            raise ValueError("Repeated machine finding")
        seen.add(identity)
    return report


def render_report(report: Mapping[str, Any]) -> str:
    body = MARKER + json.dumps(dict(report), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    parse_report(body)
    return body


def build_report(pull, *, path: str, locale: str, findings: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    report = dict(version=1, repository=pull.repository,
                  repository_id=pull.base_repository_id, pull_id=pull.pull_id,
                  pull_number=pull.number, head_repository_id=pull.head_repository_id,
                  head_sha=pull.head_sha, base_sha=pull.base_sha, path=path,
                  locale=locale, findings=list(findings))
    return parse_report(render_report(report))


def report_matches_pull(report, pull, *, expected_head_sha=None) -> bool:
    heads = {pull.head_sha}
    if isinstance(expected_head_sha, str):
        heads.add(expected_head_sha)
    elif expected_head_sha is not None:
        heads.update(expected_head_sha)
    if report["head_sha"] not in heads:
        return False
    return all(report[name] == value for name, value in {
        "repository": pull.repository, "repository_id": pull.base_repository_id,
        "pull_id": pull.pull_id, "pull_number": pull.number,
        "head_repository_id": pull.head_repository_id,
        "base_sha": pull.base_sha,
    }.items())


def verify_report_values(report, values, *, brands=(), ignored_patterns=(), prevention_only=False):
    """Return exact allowed keys only after independent deterministic checks."""
    from localize.ignore_keys import compile_ignore_key_patterns, is_ignored_key

    patterns = compile_ignore_key_patterns(ignored_patterns)
    allowed = set()
    for item in report["findings"]:
        identity = (report["path"], item["key"])
        pair = values.get(identity)
        if pair is None or is_ignored_key(item["key"], patterns):
            raise ValueError("Machine finding has no authorized current key")
        source, target = pair
        if (value_digest(source) != item["source_sha256"]
                or (not prevention_only and (
                    value_digest(target) != item["target_sha256"]
                    or item["category"] not in deterministic_categories(item["key"], source, target, brands)))):
            raise ValueError("Machine finding does not reproduce in current evidence")
        if not prevention_only:
            allowed.add(identity)
    return frozenset(allowed)
