"""Trusted glossary rejection uses the real bounded driver before caching."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from localize.guardian import codex
from localize.guardian.models import CodexAuthMode, GuardianMode, GuardianRuntime
from localize.guardian.state import GuardianState
from tests.unit.test_guardian_controller import (
    COMMIT_SHA,
    NOW,
    TARGET_PATH,
    _config,
    _controller,
    runtime,
)

controller_runtime = runtime
SOURCE = "Where should the bitcoin go?"
INVALID = "Куда должны поступить биткоины?"
VALID = "Куда отправить Bitcoin?"


def _payload(feedback_id, value):
    return {
        "schema_version": 1,
        "summary": "A localized address prompt.",
        "feedback": [
            {
                "feedback_id": feedback_id,
                "verdict": "apply",
                "confidence": 0.99,
                "rationale": "Use the configured terminology.",
                "replacements": [
                    {
                        "path": TARGET_PATH,
                        "key": "greeting",
                        "expected_value": SOURCE,
                        "proposed_value": value,
                    }
                ],
            }
        ],
        "recurrence_candidates": [],
    }


def _setup(
    tmp_path,
    monkeypatch,
    controller_runtime,
    state,
    values,
    *,
    daily_limit=50,
    locale_glossary=False,
    enforcement="exact",
):
    base, head, checkout, provider, broker, sequence = controller_runtime
    (base / "l10n/messages_en.properties").write_text("greeting=" + SOURCE + "\n")
    (head / TARGET_PATH).write_text("greeting=" + SOURCE + "\n")
    config_path = base / ".localize/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["translation_glossary_enforcement"] = enforcement
    if locale_glossary:
        (base / ".localize/glossary.json").write_text(
            json.dumps({"ru": {"bitcoin": "Bitcoin"}})
        )
    else:
        config["brand_technical_glossary"] = ["Bitcoin"]
    config_path.write_text(yaml.safe_dump(config))
    calls = []
    proposals = iter(values)

    def process(argv, **kwargs):
        calls.append(kwargs["input"])
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "CODEX_API_KEY" not in kwargs["env"]
        evidence = Path(argv[argv.index("-C") + 1])
        feedback_id = json.loads((evidence / "manifest.json").read_text())[
            "feedback_ids"
        ][0]
        Path(argv[argv.index("-o") + 1]).write_text(
            json.dumps(_payload(feedback_id, next(proposals)))
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
            "",
        )

    monkeypatch.setattr(codex, "run_bounded_process", process)
    driver = codex.CodexDriver(
        model="test-model", codex_home=tmp_path / "subscription", max_attempts=2
    )
    guardian_config = _config(GuardianMode.APPLY_OWNED_TRANSLATIONS)
    guardian_config = replace(
        guardian_config,
        limits=replace(guardian_config.limits, max_model_calls_per_day=daily_limit),
        runtime=GuardianRuntime(codex_auth_mode=CodexAuthMode.CHATGPT),
    )
    controller = _controller(
        tmp_path=tmp_path,
        state=state,
        config=guardian_config,
        checkout=checkout,
        provider=provider,
        broker=broker,
        driver=driver,
    )
    return controller, calls, sequence, provider


@pytest.mark.parametrize("locale_glossary", [False, True])
def test_invalid_glossary_result_retries_before_cache_and_applies_compliant_result(
    tmp_path,
    monkeypatch,
    controller_runtime,
    locale_glossary,
):
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller, calls, sequence, _ = _setup(
            tmp_path,
            monkeypatch,
            controller_runtime,
            state,
            [INVALID, VALID],
            locale_glossary=locale_glossary,
        )
        outcome = controller.poll_once()
        assert outcome.applied_commits == (COMMIT_SHA,)
        assert len(calls) == 2
        assert "glossary" in calls[1] and "brand" in calls[1]
        assert INVALID not in calls[1]
        assert state.model_calls_committed_for_day(NOW.date()) == 2
        assert state.cost_for_day(NOW.date()) == 0
        cached = state._connection.execute(
            "SELECT result_json FROM assessment_results"
        ).fetchall()
        assert (
            len(cached) == 1 and VALID in cached[0][0] and INVALID not in cached[0][0]
        )
        assert sequence.count("publish") == 1


@pytest.mark.parametrize(
    "daily_limit,values,expected_calls",
    [(50, [INVALID, INVALID], 2), (1, [INVALID], 1)],
)
def test_glossary_retries_preserve_attempt_and_shared_daily_limits(
    tmp_path,
    monkeypatch,
    controller_runtime,
    daily_limit,
    values,
    expected_calls,
):
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller, calls, sequence, _ = _setup(
            tmp_path,
            monkeypatch,
            controller_runtime,
            state,
            values,
            daily_limit=daily_limit,
        )
        outcome = controller.poll_once()
        assert not outcome.applied_commits
        assert len(calls) == expected_calls
        assert state.model_calls_committed_for_day(NOW.date()) == expected_calls
        assert (
            state._connection.execute(
                "SELECT COUNT(*) FROM assessment_results"
            ).fetchone()[0]
            == 0
        )
        assert "publish" not in sequence


def test_invalid_retained_cache_is_evicted_before_new_assessment(
    tmp_path, monkeypatch, controller_runtime
):
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller, calls, _, provider = _setup(
            tmp_path, monkeypatch, controller_runtime, state, [VALID]
        )
        feedback = provider.snapshots[0].feedback[0]
        bad = json.dumps(
            _payload(f"{feedback.kind.value}:{feedback.source_id}", INVALID)
        )
        lookup = state.cached_assessment_result
        evictions = []
        invalidate = state.invalidate_assessment_result

        def seed_old_cache(**kwargs):
            state.cache_assessment_result(**kwargs, result_json=bad, created_at=NOW)
            return lookup(**kwargs)

        def evict(**kwargs):
            evictions.append(kwargs["result_json"])
            return invalidate(**kwargs)

        monkeypatch.setattr(state, "cached_assessment_result", seed_old_cache)
        monkeypatch.setattr(state, "invalidate_assessment_result", evict)
        assert controller.poll_once().applied_commits == (COMMIT_SHA,)
        assert evictions == [bad]
        assert len(calls) == 1


def test_nonexact_glossary_policy_is_not_silently_strengthened(
    tmp_path, monkeypatch, controller_runtime
):
    with GuardianState(tmp_path / "state.sqlite3") as state:
        controller, calls, _, _ = _setup(
            tmp_path,
            monkeypatch,
            controller_runtime,
            state,
            [INVALID],
            enforcement="off",
        )
        controller.poll_once()
        assert len(calls) == 1
        assert (
            state._connection.execute(
                "SELECT COUNT(*) FROM assessment_results"
            ).fetchone()[0]
            == 1
        )
