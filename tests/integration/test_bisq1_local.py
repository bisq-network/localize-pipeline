"""Run the local job through its real pipeline and quality gate without a model."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("reject_quality", [False, True])
@pytest.mark.parametrize("locales", [["de"], ["pt", "pt-br"]])
def test_manual_job_keeps_source_and_promotes_state_only_after_quality(tmp_path, reject_quality, locales):
    state = tmp_path / "state"
    repo = state / "bisq"
    resources = repo / "core/src/main/resources/i18n"
    resources.mkdir(parents=True)
    source = resources / "displayStrings.properties"
    source.write_text("label=Account\n")
    for code, value in {"de": "Konto", "pt": "Conta", "pt-br": "Conta", "fr": "Compte"}.items():
        (resources / f"displayStrings_{code}.properties").write_text(f"label={value}\n")
    from localize.translation_memory import TranslationMemory, write_translation_memory
    reviewed_memory = TranslationMemory()
    reviewed_memory.record("Account", "Konto", locale="de", format_id="java_properties")
    write_translation_memory(state / "reviewed-memory.json", reviewed_memory)
    memory_before = (state / "reviewed-memory.json").read_bytes()
    # A previous interrupted invocation must not feed unrelated locales into
    # this run, or copy an old output back over an unselected target.
    for folder in ("queue", "translated"):
        (state / folder).mkdir()
        (state / folder / "displayStrings_it.properties").write_text("label=Stale\n")

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
original_run = job.asyncio.run
observed = []
def observe_run(coro):
    from localize import translate_localization_files as runtime
    original_process = runtime.process_translation_queue
    async def observe_queue(*args, **kwargs):
        queue = Path(kwargs.get("translation_queue_folder") or args[0])
        output = Path(kwargs.get("translated_queue_folder") or args[1])
        observed.append({
            "input": sorted(str(path.relative_to(queue)) for path in Path(queue).rglob("*.properties")),
            "output": sorted(str(path.relative_to(output)) for path in Path(output).rglob("*.properties")),
        })
        import json
        (Path(sys.argv[2]) / "queue-observed.json").write_text(json.dumps(observed))
        return await original_process(*args, **kwargs)
    runtime.process_translation_queue = observe_queue
    return original_run(coro)
job.asyncio.run = observe_run
if sys.argv[4] == "reject":
    import localize.translation_quality_gate
    localize.translation_quality_gate.main = lambda args: 1
state_path = sys.argv[2]
base = sys.argv[3]
locales = sys.argv[5:]
sys.argv = ["job", "--work-dir", state_path, "--base-ref", base,
            "--resume-memory", str(Path(state_path) / "reviewed-memory.json"), "--locales", *locales]
raise SystemExit(job.main())
"""
    env = dict(os.environ)
    env.update({
        "OPENAI_API_KEY": "test-only",
        "OPENAI_BASE_URL": "http://127.0.0.1:1",
    })
    result = subprocess.run(
        [sys.executable, "-c", harness, str(ROOT / "scripts/run-bisq1-local.py"),
         str(state), base, "reject" if reject_quality else "accept", *locales],
        env=env, text=True, capture_output=True, timeout=30,
    )
    queued = json.loads((state / "queue-observed.json").read_text())[0]
    expected = sorted(f"displayStrings_{code}.properties" for code in locales)
    assert queued == {"input": expected, "output": []}
    assert result.returncode == int(reject_quality), result.stdout + result.stderr
    report_dir = Path((state / "latest-run.txt").read_text().strip())
    if not reject_quality:
        assert json.loads((report_dir / "quality.json").read_text())["blocking"] is False
    assert (report_dir / "translations.patch").read_bytes() == b""
    assert (state / "ledger.json").is_file() is not reject_quality
    assert (state / "memory.json").is_file() is not reject_quality
    assert (report_dir / "memory.json").is_file()
    assert (state / "reviewed-memory.json").read_bytes() == memory_before
    assert (report_dir / "ledger.json").is_file()
    assert source.read_bytes() == before
    assert set(json.loads((report_dir / "plan.json").read_text())["files"]) == set(expected)
    for folder in ("queue", "translated"):
        assert (state / folder / "displayStrings_it.properties").read_text() == "label=Stale\n"
    assert git("diff", "--cached", "--name-only") == ""
    usage = json.loads((report_dir / "logs/token_usage_summary.json").read_text())
    assert usage["totals"]["calls"] == 0
