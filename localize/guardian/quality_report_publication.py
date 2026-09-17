"""Publish typed, revision-bound quality evidence for an owned translation PR.

This can also backfill an existing PR: materialize its exact head, supply the
trusted operator config, and pass its current expected head explicitly. No
model calls are made and existing comments are never edited.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from localize.guardian.github import _parse_pull_request
from localize.guardian.quality_reports import build_report, deterministic_categories, finding, render_report
from localize.ignore_keys import is_ignored_key
from localize.translation_quality_gate import (
    _deduplicate_translation_changes, _iter_profile_translation_changes,
    load_quality_gate_config, load_quality_gate_localization_profiles,
)

MAX_REPORTS = 100


def reports_from_changes(pull, changes, *, brands=(), ignored_patterns=()):
    """One public report per file; Guardian derives resumable per-key events."""
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
    reports = tuple(build_report(pull, path=path, locale=locale, findings=items[start:start + 100])
                 for (path, locale), items in sorted(grouped.items())
                 for start in range(0, len(items), 100))
    if len(reports) > MAX_REPORTS:
        raise ValueError("Machine report chunks exceed the publication bound; split the PR.")
    return reports


def _run(argv, *, cwd=None, input_text=None):
    result = subprocess.run(argv, cwd=cwd, input=input_text, text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("Quality evidence command failed; no credentials or command output are included.")
    return result.stdout


def publish_reports(*, repository, pull_number, expected_head, reports, request):
    """Check exact head and numeric producer identity; replay never duplicates."""
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
    published = 0
    for report in reports:
        fresh = _parse_pull_request(repository, request("GET", f"/repos/{repository}/pulls/{pull_number}"))
        if fresh.state != "open" or fresh.head_sha != expected_head or build_report(
                fresh, path=report["path"], locale=report["locale"], findings=report["findings"]) != report:
            raise ValueError("Pull revision changed before machine evidence publication")
        body = render_report(report)
        if body in owned:
            continue
        created = request("POST", endpoint, {"body": body})
        if (created.get("body") != body or created.get("user", {}).get("id") != actor["id"]
                or created.get("user", {}).get("type") != actor["type"]):
            raise ValueError("Machine evidence publication could not be verified")
        owned.add(body)
        published += 1
    return {"finding_count": sum(len(report["findings"]) for report in reports),
            "report_count": len(reports), "published": published,
            "already_present": len(reports) - published}


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
