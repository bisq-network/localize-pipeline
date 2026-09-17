"""Publish a human-readable quality summary for an owned translation PR.

This can also backfill an existing PR: materialize its exact head, supply the
trusted operator config, and pass its current expected head explicitly. No
model calls are made and existing comments are never edited. Machine evidence
stays internal; GitHub comments contain no hidden payloads or raw reports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from localize.guardian import quality_reports
from localize.guardian.github import _parse_pull_request
from localize.guardian.quality_reports import build_report, deterministic_categories, finding, render_report
from localize.ignore_keys import is_ignored_key
from localize.translation_quality_gate import (
    _deduplicate_translation_changes, _iter_profile_translation_changes,
    load_quality_gate_config, load_quality_gate_localization_profiles,
)

MAX_REPORTS = 100


def reports_from_changes(pull, changes, *, brands=(), ignored_patterns=()):
    """Build bounded internal reports for validation and summary accounting."""
    grouped = defaultdict(list)
    for change in changes:
        if change.source_value is None or is_ignored_key(change.key, ignored_patterns):
            continue
        for category in deterministic_categories(change.key, change.source_value, change.new_value, brands):
            grouped[(change.file, change.locale_code)].append(
                finding(change.key, change.source_value, change.new_value, category)
            )
    if len(grouped) > MAX_REPORTS:
        raise ValueError(f"Machine report count exceeds {MAX_REPORTS}; split the translation PR before reporting.")
    reports = []

    def append_chunk(report):
        if len(reports) >= MAX_REPORTS:
            raise ValueError("Machine report chunks exceed the publication bound; split the PR.")
        # Revalidate the complete chunk, including duplicate finding identities.
        render_report(report)
        reports.append(report)

    for (path, locale), items in sorted(grouped.items()):
        chunk = None
        chunk_bytes = 0
        for item in items:
            try:
                single = build_report(pull, path=path, locale=locale, findings=[item])
            except ValueError as exc:
                raise ValueError("A single machine finding cannot fit the report limits.") from exc
            single_bytes = len(render_report(single).encode("utf-8"))
            # The canonical JSON array gains one comma plus this encoded object.
            # Count bytes, including escaped characters, rather than key length.
            added_bytes = 1 + len(json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"))
            if chunk is not None and (
                len(chunk["findings"]) >= quality_reports.MAX_FINDINGS
                or chunk_bytes + added_bytes > quality_reports.MAX_REPORT_BYTES
            ):
                append_chunk(chunk)
                chunk = None
            if chunk is None:
                chunk = single
                chunk_bytes = single_bytes
            else:
                chunk["findings"].append(item)
                chunk_bytes += added_bytes
        if chunk is not None:
            append_chunk(chunk)
    return tuple(reports)


def _run(argv, *, cwd=None, input_text=None):
    result = subprocess.run(argv, cwd=cwd, input=input_text, text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("Quality evidence command failed; no credentials or command output are included.")
    return result.stdout


def publish_reports(*, repository, pull_number, expected_head, reports, request):
    """Publish at most one owned summary; counters distinguish reports/comments."""
    reports = tuple(reports)
    counts = {"finding_count": sum(len(report["findings"]) for report in reports),
              "report_count": len(reports), "published": 0, "already_present": 0}
    if not reports:
        return counts
    if len(reports) > MAX_REPORTS:
        raise ValueError("Quality summary exceeds the internal report bound")
    for report in reports:
        render_report(report)
    actor = request("GET", "/user")
    if type(actor.get("id")) is not int or actor.get("type") not in {"User", "Bot"}:
        raise ValueError("Unrecognized quality evidence producer")
    endpoint = f"/repos/{repository}/issues/{pull_number}/comments"
    existing = request("PAGINATE", endpoint)
    if len(existing) > 1000:
        raise ValueError("Quality evidence comment listing exceeded its bound")
    owned = {item.get("body") for item in existing
             if item.get("user", {}).get("id") == actor["id"]
             and item.get("user", {}).get("type") == actor["type"]}
    fresh = _parse_pull_request(repository, request("GET", f"/repos/{repository}/pulls/{pull_number}"))
    for report in reports:
        if fresh.state != "open" or fresh.head_sha != expected_head or build_report(
                fresh, path=report["path"], locale=report["locale"], findings=report["findings"]) != report:
            raise ValueError("Pull revision changed before quality summary publication")
    locale_counts = Counter()
    categories = Counter()
    for report in reports:
        locale_counts[report["locale"]] += len(report["findings"])
        categories.update(item["category"] for item in report["findings"])
    noun = "finding" if counts["finding_count"] == 1 else "findings"
    body = (
        "Automated translation quality check\n\n"
        f"At [{expected_head[:7]}](https://github.com/{repository}/commit/{expected_head}), "
        f"the check flagged {counts['finding_count']} candidate {noun} for review. "
        "These are not confirmed defects.\n\n"
        "Locales: " + ", ".join(f"{locale}: {count}" for locale, count in sorted(locale_counts.items())) + ".\n\n"
    )
    if categories["source_echo"]:
        body += (f"- Source-identical values: {categories['source_echo']}. Check whether translation is needed; "
                 "product names and shared-language terms may legitimately remain unchanged.\n")
    if categories["control_character"]:
        body += (f"- Values with control characters: {categories['control_character']}. "
                 "Check whether those characters belong in the displayed text.\n")
    body += "\nThis check does not change translations or approve the PR."
    if body in owned:
        counts["already_present"] = 1
        return counts
    created = request("POST", endpoint, {"body": body})
    if (created.get("body") != body or created.get("user", {}).get("id") != actor["id"]
            or created.get("user", {}).get("type") != actor["type"]):
        raise ValueError("Quality summary publication could not be verified")
    counts["published"] = 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository", "pull-number", "expected-head", "repo-root", "config", "input-folder"):
        parser.add_argument("--" + name, required=True, type=int if name == "pull-number" else str)
    args = parser.parse_args(argv)

    def request(method, endpoint, data=None):
        command = ["gh", "api", endpoint]
        if method == "PAGINATE":
            items = []
            for page in range(1, 12):
                batch = json.loads(_run(["gh", "api", f"{endpoint}?per_page=100&page={page}"]))
                if not isinstance(batch, list) or len(batch) > 100:
                    raise ValueError("Malformed machine evidence comment page")
                items.extend(batch)
                if len(items) > 1000:
                    raise ValueError("Machine evidence comments exceed the intake bound")
                if len(batch) < 100:
                    return items
            raise ValueError("Machine evidence comments exceeded the page bound")
        if method == "POST":
            return json.loads(_run(command + ["--method", "POST", "--input", "-"], input_text=json.dumps(data)))
        return json.loads(_run(command))

    root = Path(args.repo_root).resolve()
    pull = _parse_pull_request(args.repository, request("GET", f"/repos/{args.repository}/pulls/{args.pull_number}"))
    if pull.state != "open" or pull.head_sha != args.expected_head or _run(["git", "rev-parse", "HEAD"], cwd=root).strip() != args.expected_head:
        raise ValueError("Machine evidence requires the exact current PR head")
    config, locales, brands, _rules = load_quality_gate_config(args.config)
    profiles = load_quality_gate_localization_profiles(args.config)
    input_folder = Path(args.input_folder).resolve()
    input_prefix = input_folder.relative_to(root)
    changed_paths = _run(["git", "diff", "--name-only", "-z", pull.base_sha, pull.head_sha, "--"], cwd=root).split("\0")
    evidence_paths = {path for path in changed_paths if path}
    source_paths = set()
    for path in tuple(evidence_paths):
        try:
            relative = Path(path).relative_to(input_prefix).as_posix()
        except ValueError:
            continue
        for profile in profiles:
            if profile.localization_layout.extract_locale(relative, locales, profile.localization_format):
                source_paths.add((input_prefix / profile.localization_layout.source_path_for_target(
                    relative, locales, profile.localization_format)).as_posix())
    evidence_paths.update(source_paths)
    # Other pending translation batches may remain dirty; only the exact
    # report's target/source files are read and must match their committed bytes.
    if evidence_paths and _run(["git", "diff", "HEAD", "--", *sorted(evidence_paths)], cwd=root).strip():
        raise ValueError("Machine evidence target/source files are not at the exact committed revision")
    if source_paths and _run(["git", "diff", pull.base_sha, pull.head_sha, "--", *sorted(source_paths)], cwd=root).strip():
        raise ValueError("Machine evidence source files differ from the trusted base")
    diff = _run(["git", "diff", "--no-ext-diff", "--no-textconv", pull.base_sha, pull.head_sha, "--"], cwd=root)
    if len(diff.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Translation diff exceeds the machine evidence bound")
    changes = _deduplicate_translation_changes(_iter_profile_translation_changes(
        diff, str(root), args.input_folder, locales, profiles,
    ))
    changes = [replace(change, file=(input_prefix / change.file).as_posix()) for change in changes]
    reports = reports_from_changes(pull, changes, brands=brands, ignored_patterns=config.ignore_key_patterns)
    print(json.dumps(publish_reports(repository=args.repository, pull_number=args.pull_number,
                                    expected_head=args.expected_head, reports=reports, request=request)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
