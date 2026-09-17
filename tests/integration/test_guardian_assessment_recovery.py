"""Trusted-key validation precedes cache admission and uses bounded attempts."""

import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from localize.guardian import codex
from localize.guardian.models import CodexAuthMode, GuardianMode, TrustedActor
from localize.guardian.state import GuardianState
from tests.unit.test_guardian_controller import (
    NOW,
    FakeCodexDriver,
    _config,
    _controller,
    _feedback,
    _policy,
    _snapshot,
    TARGET_PATH,
    runtime,
)
from tests.integration.test_guardian_quality_intake import SOURCE, machine_snapshot

controller_runtime = runtime


@pytest.mark.parametrize("recovery", [True, False])
def test_duplicate_replacements_receive_fixed_guidance_before_bounded_retry(
    tmp_path, monkeypatch, controller_runtime, recovery,
):
    _base, head, checkout, provider, broker, _sequence = controller_runtime
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n")
    provider.snapshots = (_snapshot(feedback=(_feedback(source_id="44"), machine_snapshot().feedback[0])),)
    policy = replace(_policy(), quality_report_actor=TrustedActor("producer", 8, "User"))
    config = _config(GuardianMode.OBSERVE, policies=(policy,))
    prompts = []
    with GuardianState(tmp_path / "state.sqlite3") as state:
        def process(argv, **kwargs):
            prompts.append(kwargs["input"])
            task = codex.CodexTask(prompt="test", evidence_dir=Path(argv[argv.index("-C") + 1]))
            payload = json.loads(codex.serialize_codex_result(FakeCodexDriver().run(task)))
            payload["feedback"][0]["replacements"][0]["expected_value"] = SOURCE
            second = json.loads(json.dumps(payload["feedback"][0]))
            second["feedback_id"] = json.loads((task.evidence_dir / "manifest.json").read_text())["feedback_ids"][1]
            if len(prompts) == 1 or not recovery:
                payload["summary"] = "private-output-marker"
            else:
                second["replacements"] = []
                second["verdict"] = "reject"
            payload["feedback"].append(second)
            # A failed first response must never have reached the durable cache.
            assert state._connection.execute("SELECT COUNT(*) FROM assessment_results").fetchone()[0] == 0
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(payload))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(codex, "run_bounded_process", process)
        controller = _controller(
            tmp_path=tmp_path, state=state, config=config, checkout=checkout,
            provider=provider, broker=broker,
            driver=codex.CodexDriver(model="test-model", max_attempts=2,
                                     auth_mode=config.runtime.codex_auth_mode),
        )
        outcome = controller.poll_once()
        assert len(prompts) == 2
        assert "entire feedback array" in prompts[0]
        assert "one replacement per (path, key)" in prompts[0]
        assert "previous result failed schema or semantic validation" in prompts[1]
        assert "one replacement per (path, key)" in prompts[1]
        assert "private-output-marker" not in prompts[1]
        assert "l10n/messages_ru.properties:greeting" not in prompts[1]
        assert state.model_calls_committed_for_day(NOW.date()) == 2
        cached = state._connection.execute("SELECT result_json FROM assessment_results").fetchall()
        if recovery:
            assert outcome.runs_completed == 1
            assert outcome.runs_failed == 0
            assert len(cached) == 1 and "private-output-marker" not in cached[0][0]
        else:
            assert outcome.runs_failed == 1
            assert cached == []


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("auth_mode", [CodexAuthMode.CHATGPT, CodexAuthMode.API_KEY])
@pytest.mark.parametrize("recovery", ["valid", "invalid", "daily_limit", "quota", "auth"])
def test_invalid_assessment_recovery_is_bounded_and_accounted(
    tmp_path, monkeypatch, controller_runtime, cached, recovery, auth_mode,
):
    """Exercise real controller, evidence, driver retries and durable accounting."""
    _base, _head, checkout, provider, broker, _sequence = controller_runtime
    config = _config(GuardianMode.OBSERVE)
    config = replace(
        config,
        runtime=replace(
            config.runtime, codex_auth_mode=auth_mode,
            codex_api_key_command=(
                config.runtime.codex_api_key_command
                if auth_mode is CodexAuthMode.API_KEY else ()
            ),
        ),
        limits=replace(
            config.limits,
            max_model_calls_per_day=(1 + int(cached) if recovery == "daily_limit" else 50),
        ),
    )
    prompts = []
    with GuardianState(tmp_path / "state.sqlite3") as state:
        if cached:
            # Reproduce a cache created by the old version before conversion.
            seed = _controller(
                tmp_path=tmp_path, state=state, config=config, checkout=checkout,
                provider=provider, driver=FakeCodexDriver(), broker=broker,
            )

            def crash_after_persistence(*args, **kwargs):
                raise RuntimeError("simulated post-cache crash")

            seed.assessment_converter = crash_after_persistence
            assert seed.poll_once().runs_failed == 1
            row = state._connection.execute("SELECT * FROM assessment_results").fetchone()
            poisoned = json.loads(row["result_json"])
            poisoned["feedback"][0]["replacements"][0]["key"] = "greeting.typo"
            state._connection.execute(
                "UPDATE assessment_results SET result_json=? WHERE cache_key=?",
                (json.dumps(poisoned), row["cache_key"]),
            )
            state._connection.commit()

        def process(argv, **kwargs):
            prompts.append(kwargs["input"])
            if recovery in {"quota", "auth"} and len(prompts) == 2:
                return subprocess.CompletedProcess(
                    argv, 1, "",
                    "You've hit your usage limit" if recovery == "quota"
                    else "401 Unauthorized: authentication failed",
                )
            task = codex.CodexTask(
                prompt="test", evidence_dir=Path(argv[argv.index("-C") + 1]),
            )
            payload = json.loads(codex.serialize_codex_result(FakeCodexDriver().run(task)))
            if len(prompts) == 1 or recovery == "invalid":
                payload["feedback"][0]["replacements"][0]["key"] = "greeting.typo"
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(payload))
            assert ("CODEX_API_KEY" in kwargs["env"]) == (auth_mode is CodexAuthMode.API_KEY)
            assert "OPENAI_API_KEY" not in kwargs["env"]
            return subprocess.CompletedProcess(
                argv, 0,
                '{"type":"turn.completed","usage":{"input_tokens":10,'
                '"output_tokens":2},"cost_usd":0.01}\n', "",
            )

        monkeypatch.setattr(codex, "run_bounded_process", process)
        controller = _controller(
            tmp_path=tmp_path, state=state, config=config, checkout=checkout,
            provider=provider, broker=broker,
            driver=codex.CodexDriver(model="test-model", max_attempts=2, auth_mode=auth_mode),
        )
        outcome = controller.poll_once()
        expected_calls = 1 if recovery == "daily_limit" else 2
        assert len(prompts) == expected_calls
        assert state.model_calls_committed_for_day(NOW.date()) == expected_calls + int(cached)
        known_cost = (
            Decimal("0.25") * int(cached)
            + Decimal("0.01") * (expected_calls - int(recovery in {"quota", "auth"}))
            if auth_mode is CodexAuthMode.API_KEY else 0
        )
        assert state.cost_for_day(NOW.date()) == known_cost
        assert not outcome.applied_commits
        rows = state._connection.execute("SELECT result_json FROM assessment_results").fetchall()
        if recovery == "valid":
            assert outcome.runs_completed == 1
            assert outcome.runs_failed == 0
            assert len(rows) == 1
            assert "greeting.typo" not in rows[0][0]
            # Recovery does not disable reuse of a valid durable result.
            assert controller.poll_once().runs_failed == 0
            assert len(prompts) == expected_calls
        else:
            assert outcome.runs_failed == 1
            assert rows == []
            assert state.pending_event_revisions(mode=GuardianMode.OBSERVE)
        if cached:
            record = state.latest_health("guardian-assessment-cache")
            assert record.details["cache_key"] == row["cache_key"]
            assert record.details["reason"] == "invalid_cached_assessment"
            assert "greeting.typo" not in json.dumps(record.details)
        if len(prompts) == 2:
            assert "exact keys" in prompts[1]
            assert "greeting.typo" not in prompts[1]


def test_cache_invalidation_is_exact_and_preserves_other_entries(tmp_path):
    """A stale invalidation cannot remove a replacement result or another task."""
    with GuardianState(tmp_path / "state.sqlite3") as state:
        for key in ("a" * 64, "b" * 64):
            state.cache_assessment_result(
                cache_key=key, repository="acme/widgets", pr_number=1,
                head_sha="a" * 40, base_sha="b" * 40, model="test-model",
                reasoning_effort="high", result_json='{"summary":"current"}',
            )
        with pytest.raises(RuntimeError, match="changed before invalidation"):
            state.invalidate_assessment_result(cache_key="a" * 64, result_json="{}")
        assert state.latest_health("guardian-assessment-cache") is None
        assert state._connection.execute("SELECT COUNT(*) FROM assessment_results").fetchone()[0] == 2
        state.invalidate_assessment_result(
            cache_key="a" * 64, result_json='{"summary":"current"}',
        )
        rows = state._connection.execute("SELECT cache_key FROM assessment_results").fetchall()
        assert [row[0] for row in rows] == ["b" * 64]
        assert state.latest_health("guardian-assessment-cache").details["cache_key"] == "a" * 64
