"""Run the local job through its real pipeline and quality gate without a model."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("reject_quality", [False, True])
def test_manual_job_keeps_source_and_promotes_state_only_after_quality(tmp_path, reject_quality):
    state = tmp_path / "state"
    repo = state / "bisq"
    resources = repo / "core/src/main/resources/i18n"
    resources.mkdir(parents=True)
    source = resources / "displayStrings.properties"
    source.write_text("label=Account\n")
    (resources / "displayStrings_de.properties").write_text("label=Konto\n")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-b", "master")
    git("config", "user.name", "Local job test")
    git("config", "user.email", "test@example.invalid")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "Translation baseline")
    base = git("rev-parse", "HEAD")
    with (repo / ".git/info/exclude").open("a") as stream:
        stream.write("\n/core/src/main/resources/i18n/archive/\n")
    before = source.read_bytes()
    # Substitute only the network checkout preparation. Parsing, ledger,
    # pipeline execution, staging and the quality gate are real.
    harness = """
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("job", sys.argv[1])
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)
job.prepare_checkout = lambda repo: None
if sys.argv[4] == "reject":
    import localize.translation_quality_gate
    localize.translation_quality_gate.main = lambda args: 1
sys.argv = ["job", "--work-dir", sys.argv[2], "--base-ref", sys.argv[3], "--locales", "de"]
raise SystemExit(job.main())
"""
    env = dict(os.environ)
    env.update({
        "OPENAI_API_KEY": "test-only",
        "OPENAI_BASE_URL": "http://127.0.0.1:1",
    })
    result = subprocess.run(
        [sys.executable, "-c", harness, str(ROOT / "scripts/run-bisq1-local.py"),
         str(state), base, "reject" if reject_quality else "accept"],
        env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == int(reject_quality), result.stdout + result.stderr
    report_dir = Path((state / "latest-run.txt").read_text().strip())
    if not reject_quality:
        assert json.loads((report_dir / "quality.json").read_text())["blocking"] is False
    assert (report_dir / "translations.patch").read_bytes() == b""
    assert (state / "ledger.json").is_file() is not reject_quality
    assert (report_dir / "ledger.json").is_file()
    assert source.read_bytes() == before
    assert git("diff", "--cached", "--name-only") == ""
    usage = json.loads((report_dir / "logs/token_usage_summary.json").read_text())
    assert usage["totals"]["calls"] == 0
