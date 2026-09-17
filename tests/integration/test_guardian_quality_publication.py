"""The deployed producer emits the same exact envelope Guardian consumes."""

from dataclasses import replace
import json
import subprocess

import pytest

from localize.guardian import quality_report_publication as publication
from localize.guardian.authorization import authorize_feedback
from localize.guardian.github import FeedbackKind, _parse_feedback
from localize.guardian.models import TrustedActor
from localize.guardian.quality_reports import build_report, finding, parse_report
from tests.unit.test_guardian_controller import _policy, _pull, _snapshot, _write_tree, TARGET_PATH


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
    reports = [build_report(pull, path=TARGET_PATH, locale="ru", findings=[
        finding(f"key{index}", "Hello reader", "Hello reader", "source_echo")
        for index in range(start, stop)
    ]) for start, stop in ((0, 2), (2, 5))]
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
        "report_count": 2, "finding_count": 5, "published": 2, "already_present": 0,
    }
    assert publication.publish_reports(**kwargs)["published"] == 0
    assert len(comments) == 2
    revisions = tuple(_parse_feedback(pull.repository, pull.number, FeedbackKind.ISSUE_COMMENT, comment) for comment in comments)
    policy = replace(_policy(), quality_report_actor=TrustedActor("pipeline", 8, "User"))
    authorized = authorize_feedback(policy=policy, snapshot=_snapshot(feedback=revisions),
                                    path_locales={TARGET_PATH: "ru"}, changed_locales=("ru",))
    assert len(authorized.events) == 5
    assert len({event.event_id for event in authorized.events}) == 5


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
        report = parse_report(comments[0])
        assert report["path"] == TARGET_PATH
        assert report["findings"][0]["key"] == "greeting"
        assert json.loads(capsys.readouterr().out)["finding_count"] == 1


def test_producer_refuses_overbound_chunks_without_silent_truncation(monkeypatch):
    from localize.semantic_quality import TranslationChange
    monkeypatch.setattr(publication, "MAX_REPORTS", 1)
    changes = [TranslationChange(TARGET_PATH, "ru", f"key{index}", "Hello reader", None, "Hello reader") for index in range(101)]
    with pytest.raises(ValueError, match="chunks exceed"):
        publication.reports_from_changes(_pull(), changes)
