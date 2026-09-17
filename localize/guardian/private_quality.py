"""Derive private quality evidence from an authorized immutable PR diff.

These events never come from GitHub comments. Their source and target bytes
are read from exact checkouts, and the normal assessment/publication guards
still decide whether a candidate warrants an edit.
"""

from dataclasses import replace
import json

from localize.formats import get_localization_adapter, load_localization_format
from localize.guardian.evidence import (
    _localization_payload, _safe_relative_file, _yaml_mapping,
)
from localize.guardian.models import FeedbackEvent
from localize.guardian.quality_reports import (
    MARKER, build_report, deterministic_categories, finding, parse_report,
    render_report, value_digest,
)
from localize.ignore_keys import compile_ignore_key_patterns, is_ignored_key

MAX_PRIVATE_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_PRIVATE_FINDINGS = 10_000


def lineage_finding_identity(body, *, allowed_heads):
    """Match exact evidence across our recorded heads, not arbitrary ancestry.

The same defect can be reported between two Guardian corrections. Only its
head label may differ; base, repository identities, key and value digests must
remain identical before a prior correction can suppress another edit.
    """
    report = parse_report(body)
    if report["head_sha"] not in allowed_heads:
        raise ValueError("Private finding is outside the durable publication lineage.")
    del report["head_sha"]
    return value_digest(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def derive_private_findings(*, policy, pull, evidence_head_sha, head_root,
                            base_root, scope, profiles, locale_codes,
                            legacy_events=(), web_base_url="https://github.com"):
    """Scan changed target values only; configured producer enables the scan.

An existing legacy report keeps its event identity during migration. It is
still independently verified by the controller before assessment. New findings
are private events, never projections of arbitrary public text.
    """
    actor = policy.quality_report_actor
    if actor is None or pull.state != "open":
        return ()
    config = _yaml_mapping(scope.config_path)
    ignored_patterns = compile_ignore_key_patterns(config.get("ignore_key_patterns"))
    payload, _paths, _locales = _localization_payload(
        repo_root=head_root, source_root=base_root,
        paths=tuple(sorted(scope.path_locales)),
        allowed_path_globs=policy.allowed_path_globs,
        profiles=profiles, locale_codes=locale_codes,
        max_file_bytes=MAX_PRIVATE_EVIDENCE_BYTES,
    )
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PRIVATE_EVIDENCE_BYTES:
        raise ValueError("Private quality evidence exceeds its byte bound.")
    legacy_keys = set()
    for event in legacy_events:
        if event.body.startswith(MARKER):
            report = parse_report(event.body)
            legacy_keys.update((report["path"], item["key"]) for item in report["findings"])
    events = []
    evidence_bytes = 0
    for file_data in payload:
        path, locale = file_data["path"], file_data["locale"]
        base_values = {}
        if scope.changed_files[path].status != "added":
            _normalized, base_file = _safe_relative_file(
                path, repo_root=base_root, allowed_path_globs=policy.allowed_path_globs,
                max_bytes=MAX_PRIVATE_EVIDENCE_BYTES,
            )
            adapter = get_localization_adapter(load_localization_format(file_data["format"]))
            _lines, base_values = adapter.parse_file(str(base_file))
        for key, pair in sorted(file_data["entries"].items()):
            source, target = pair["source"], pair["target"]
            if ((path, key) in legacy_keys or base_values.get(key) == target
                    or is_ignored_key(key, ignored_patterns)):
                continue
            categories = deterministic_categories(
                key, source, target, config.get("brand_technical_glossary") or (),
            )
            if not categories:
                continue
            report = build_report(
                replace(pull, head_sha=evidence_head_sha), path=path, locale=locale,
                findings=[finding(key, source, target, category) for category in categories],
            )
            body = render_report(report)
            evidence_bytes += len(body.encode("utf-8"))
            if evidence_bytes > MAX_PRIVATE_EVIDENCE_BYTES:
                raise ValueError("Private quality reports exceed their byte bound.")
            events.append(FeedbackEvent(
                repository=policy.base_repo, pr_number=pull.number,
                kind="quality_finding", event_id="internal:quality:" + value_digest(path + "\x00" + key),
                author=actor.login, author_id=actor.id, author_type=actor.type,
                body=body, head_sha=pull.head_sha, base_sha=pull.base_sha,
                locale=locale, path=path, updated_at=None,
                html_url=f"{web_base_url}/{policy.base_repo}/commit/{evidence_head_sha}",
            ))
            if len(events) > MAX_PRIVATE_FINDINGS:
                raise ValueError("Private quality findings exceed their count bound.")
    return tuple(events)
