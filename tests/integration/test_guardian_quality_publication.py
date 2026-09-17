"""The deployed producer publishes readable summaries, never machine evidence."""

from dataclasses import replace
import json
import subprocess

import pytest

from localize.guardian import quality_report_publication as publication
from localize.guardian.quality_reports import build_report, finding, parse_report
from tests.unit.test_guardian_controller import _pull, _write_tree, TARGET_PATH


def pull_payload(pull):
    return {
        "id": pull.pull_id, "number": pull.number, "state": pull.state,
        "created_at": pull.created_at, "updated_at": pull.updated_at,
        "user": {"id": pull.author_id, "type": pull.author_type},
        "head": {"sha": pull.head_sha, "ref": pull.head_ref,
                 "repo": {"id": pull.head_repository_id, "full_name": pull.head_repository},
                 "user": {"id": pull.head_owner_id, "type": pull.head_owner_type}},
        "base": {"sha": pull.base_sha, "ref": pull.base_ref,
                 "repo": {"id": pull.base_repository_id, "full_name": pull.repository}},
    }


def test_grouped_publication_roundtrip_dedup_and_truthful_totals():
    pull = _pull()
    reports = [build_report(pull, path=TARGET_PATH, locale=locale, findings=[
        finding(f"key{index}", "Hello reader", "Hello reader", "source_echo")
        for index in range(start, stop)
    ]) for locale, start, stop in (("ru", 0, 2), ("it", 2, 5))]
    comments = []
    actor = {"id": 8, "type": "User", "login": "pipeline"}
    def request(method, endpoint, data=None):
        if endpoint == "/user":
            return actor
        if method == "PAGINATE":
            return comments
        if method == "POST":
            comment = {"id": 100 + len(comments), "body": data["body"], "user": actor,
                       "created_at": "2026-08-30T10:00:00Z", "updated_at": "2026-08-30T10:00:00Z"}
            comments.append(comment)
            return comment
        return pull_payload(pull)
    kwargs = dict(repository=pull.repository, pull_number=pull.number, expected_head=pull.head_sha,
                  reports=reports, request=request)
    assert publication.publish_reports(**kwargs) == {
        "report_count": 2, "finding_count": 5, "published": 1, "already_present": 0,
    }
    assert publication.publish_reports(**kwargs)["published"] == 0
    assert publication.publish_reports(**kwargs)["already_present"] == 1
    assert len(comments) == 1
    body = comments[0]["body"]
    assert "🤖 **Localize Pipeline:** Please review" in body
    assert "5 source-identical values" in body
    assert len(body) < 500
    assert f"https://github.com/{pull.repository}/commit/{pull.head_sha}" in body
    assert "not confirmed defects" in body
    assert "legitimately remain unchanged" in body
    for forbidden in ("<!--", "{", "key0", "source_sha256", "repository_id", TARGET_PATH):
        assert forbidden not in body
    with pytest.raises(ValueError, match="Not a bounded machine report"):
        parse_report(body)


@pytest.mark.parametrize("dirty_target", [False, True])
def test_real_git_producer_uses_repo_paths_and_ignores_other_pending_batches(tmp_path, monkeypatch, capsys, dirty_target):
    root = tmp_path / "repo"
    root.mkdir()
    _write_tree(root)
    def git(*args):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                              cwd=root, text=True, check=True, capture_output=True).stdout.strip()
    git("init", "-q")
    (root / "unrelated.txt").write_text("Unrelated baseline\n")
    git("add", ".")
    git("commit", "-qm", "Baseline")
    base_sha = git("rev-parse", "HEAD")
    source = (root / "l10n/messages_en.properties").read_text()
    (root / TARGET_PATH).write_text(source)
    git("add", TARGET_PATH)
    git("commit", "-qm", "Translation candidate")
    head_sha = git("rev-parse", "HEAD")
    (root / "unrelated.txt").write_text("Another pending batch is untouched\n")
    if dirty_target:
        (root / TARGET_PATH).write_text(source + "new=not committed\n")
    pull = replace(_pull(), head_sha=head_sha, base_sha=base_sha)
    comments = []
    real_run = publication._run
    def run(argv, **kwargs):
        if argv[0] != "gh":
            return real_run(argv, **kwargs)
        if argv[2] == "/user":
            return json.dumps({"id": 8, "type": "User"})
        if "?per_page" in argv[2]:
            return "[]"
        if "POST" in argv:
            data = json.loads(kwargs["input_text"])
            comments.append(data["body"])
            return json.dumps({**data, "user": {"id": 8, "type": "User"}})
        return json.dumps(pull_payload(pull))
    monkeypatch.setattr(publication, "_run", run)
    args = ["--repository", pull.repository, "--pull-number", str(pull.number), "--expected-head", head_sha,
            "--repo-root", str(root), "--config", str(root / ".localize/config.yaml"), "--input-folder", str(root / "l10n")]
    if dirty_target:
        with pytest.raises(ValueError, match="not at the exact"):
            publication.main(args)
        assert not comments
    else:
        assert publication.main(args) == 0
        assert len(comments) == 1
        assert "1 source-identical value" in comments[0]
        with pytest.raises(ValueError, match="Not a bounded machine report"):
            parse_report(comments[0])
        assert json.loads(capsys.readouterr().out)["finding_count"] == 1


def test_producer_refuses_overbound_chunks_without_silent_truncation(monkeypatch):
    from localize.semantic_quality import TranslationChange
    monkeypatch.setattr(publication, "MAX_REPORTS", 1)
    changes = [TranslationChange(TARGET_PATH, "ru", f"key{index}", "Hello reader", None, "Hello reader") for index in range(101)]
    with pytest.raises(ValueError, match="chunks exceed"):
        publication.reports_from_changes(_pull(), changes)


def test_no_findings_makes_no_github_requests():
    def request(*args):
        pytest.fail("Empty quality results must not publish or query comments")
    assert publication.publish_reports(repository="acme/widgets", pull_number=12,
                                       expected_head=_pull().head_sha, reports=(), request=request) == {
        "finding_count": 0, "report_count": 0, "published": 0, "already_present": 0,
    }


@pytest.mark.parametrize("changed", ["head", "base", "repository_id", "pull_id", "closed"])
def test_stale_or_mismatched_report_never_publishes(changed):
    pull = _pull()
    report = build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("secret-internal-key", "Hello reader", "Hello reader", "source_echo"),
    ])
    payload = pull_payload(pull)
    if changed == "head":
        payload["head"]["sha"] = "f" * 40
    elif changed == "base":
        payload["base"]["sha"] = "f" * 40
    elif changed == "repository_id":
        payload["base"]["repo"]["id"] += 1
    elif changed == "pull_id":
        payload["id"] += 1
    else:
        payload["state"] = "closed"
    def request(method, endpoint, data=None):
        if endpoint == "/user":
            return {"id": 8, "type": "User"}
        if method == "PAGINATE":
            return []
        assert method != "POST"
        return payload
    with pytest.raises(ValueError, match="revision changed"):
        publication.publish_reports(repository=pull.repository, pull_number=pull.number,
                                    expected_head=pull.head_sha, reports=[report], request=request)


def test_summary_does_not_expose_keys_and_only_deduplicates_owned_comments():
    pull = _pull()
    reports = [build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("--> <script>alert(1)</script>", "Hello", "Hello", "source_echo"),
        finding("sensitive-key", "Hello", "Hello\x01", "control_character"),
    ])]
    comments = []
    actor = {"id": 8, "type": "User"}
    def request(method, endpoint, data=None):
        if endpoint == "/user":
            return actor
        if method == "PAGINATE":
            return comments
        if method == "POST":
            created = {"body": data["body"], "user": actor.copy()}
            comments.append(created)
            return created
        return pull_payload(pull)
    kwargs = dict(repository=pull.repository, pull_number=pull.number,
                  expected_head=pull.head_sha, reports=reports, request=request)
    assert publication.publish_reports(**kwargs)["published"] == 1
    body = comments[0]["body"]
    assert "1 source-identical value" in body
    assert "1 control-character finding" in body
    assert "<" not in body and "sensitive-key" not in body
    comments[0]["user"]["id"] = 9
    assert publication.publish_reports(**kwargs)["published"] == 1
    assert publication.publish_reports(**kwargs)["already_present"] == 1


@pytest.mark.parametrize("field,value", [("body", "changed"), ("user", {"id": 9, "type": "User"})])
def test_summary_publication_requires_readback_identity(field, value):
    pull = _pull()
    report = build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("greeting", "Hello", "Hello", "source_echo"),
    ])
    def request(method, endpoint, data=None):
        if endpoint == "/user":
            return {"id": 8, "type": "User"}
        if method == "PAGINATE":
            return []
        if method == "POST":
            return {"body": data["body"], "user": {"id": 8, "type": "User"}, field: value}
        return pull_payload(pull)
    with pytest.raises(ValueError, match="could not be verified"):
        publication.publish_reports(repository=pull.repository, pull_number=pull.number,
                                    expected_head=pull.head_sha, reports=[report], request=request)


def test_invalid_later_report_fails_before_any_network_request():
    pull = _pull()
    valid = build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("greeting", "Hello", "Hello", "source_echo"),
    ])
    invalid = {**valid, "locale": "ru\n<!-- hidden -->"}
    def request(*args):
        pytest.fail("Validate the complete report bundle before making requests")
    with pytest.raises(ValueError):
        publication.publish_reports(repository=pull.repository, pull_number=pull.number,
                                    expected_head=pull.head_sha, reports=[valid, invalid], request=request)


def test_boolean_producer_id_is_not_a_numeric_identity():
    pull = _pull()
    report = build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("greeting", "Hello", "Hello", "source_echo"),
    ])
    def request(method, endpoint, data=None):
        assert endpoint == "/user"
        return {"id": True, "type": "User"}
    with pytest.raises(ValueError, match="Unrecognized"):
        publication.publish_reports(repository=pull.repository, pull_number=pull.number,
                                    expected_head=pull.head_sha, reports=[report], request=request)


def test_fresh_revision_is_checked_after_comment_listing():
    pull = _pull()
    report = build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding("greeting", "Hello", "Hello", "source_echo"),
    ])
    calls = []
    def request(method, endpoint, data=None):
        calls.append((method, endpoint))
        if endpoint == "/user":
            return {"id": 8, "type": "User"}
        if method == "PAGINATE":
            return []
        assert calls[-2][0] == "PAGINATE"
        payload = pull_payload(pull)
        payload["head"]["sha"] = "f" * 40
        return payload
    with pytest.raises(ValueError, match="revision changed"):
        publication.publish_reports(repository=pull.repository, pull_number=pull.number,
                                    expected_head=pull.head_sha, reports=[report], request=request)
    assert len(calls) == 3
