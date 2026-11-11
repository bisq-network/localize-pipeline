import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from localize.run_safeguards import (
    RequestBudgetExceeded,
    RunSafeguards,
    load_safeguards,
)


def guard(tmp_path, limit=7):
    return RunSafeguards(tmp_path / "safeguards.sqlite3", limit, 3, 5, 60)


def test_budget_is_atomic_and_survives_restarts(tmp_path):
    def request(_):
        try:
            guard(tmp_path).reserve()
            return True
        except RequestBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(request, range(30))) == 7
    with pytest.raises(RequestBudgetExceeded):
        guard(tmp_path).reserve()


def test_budget_expires_only_after_rolling_24_hours(tmp_path, monkeypatch):
    monkeypatch.setattr("localize.run_safeguards.time.time", lambda: 100000)
    guard(tmp_path, 1).reserve()
    monkeypatch.setattr("localize.run_safeguards.time.time", lambda: 100001)
    with pytest.raises(RequestBudgetExceeded):
        guard(tmp_path, 1).reserve()
    monkeypatch.setattr("localize.run_safeguards.time.time", lambda: 186401)
    guard(tmp_path, 1).reserve()


def test_sample_has_its_own_small_cap(tmp_path):
    safeguards = guard(tmp_path, 100)
    safeguards.sample_max_requests = 2
    with safeguards.sample_budget():
        safeguards.reserve()
        safeguards.reserve()
        with pytest.raises(RequestBudgetExceeded):
            safeguards.reserve()
    # The sample cap does not replace or reset the shared rolling budget.
    assert safeguards.request_count() == 2


def test_cache_is_exact_and_persistent(tmp_path):
    safeguards = guard(tmp_path)
    key = safeguards.fingerprint({"source": "Send {0}", "model": "first"})
    safeguards.accept(key, {"content": "Senden {0}"})
    assert guard(tmp_path).lookup(key) == {"content": "Senden {0}"}
    assert safeguards.lookup(safeguards.fingerprint(
        {"source": "Send {1}", "model": "first"}
    )) is None
    assert safeguards.lookup(safeguards.fingerprint(
        {"source": "Send {0}", "model": "second"}
    )) is None
    assert safeguards.request_count() == 0


@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5])
def test_invalid_caps_fail_closed(tmp_path, value):
    with pytest.raises(ValueError):
        load_safeguards(
            {"run_safeguards": {"max_requests_per_24h": value}},
            str(tmp_path / "config.yaml"),
        )


@pytest.mark.asyncio
async def test_parameter_fallback_consumes_two_requests(tmp_path):
    from localize.providers.openai_compat import create_chat_completion_with_fallback

    safeguards = guard(tmp_path, 1)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        raise TypeError("unsupported parameter max_tokens")

    with pytest.raises(RequestBudgetExceeded):
        await create_chat_completion_with_fallback(
            create, model="custom", messages=[], completion_token_limit=10,
            before_request=safeguards.reserve,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_requests_are_not_refunded(tmp_path):
    from localize.providers.openai_compat import create_chat_completion_with_fallback

    safeguards = guard(tmp_path, 1)

    async def create(**kwargs):
        raise TimeoutError("unknown provider outcome")

    with pytest.raises(TimeoutError):
        await create_chat_completion_with_fallback(
            create, model="custom", messages=[], before_request=safeguards.reserve,
        )
    with pytest.raises(RequestBudgetExceeded):
        safeguards.reserve()


def test_sdk_retries_disabled_for_both_openai_routes(tmp_path):
    from localize.providers.models import create_model_provider
    safeguards = guard(tmp_path)
    with patch("localize.providers.models.AsyncOpenAI") as client:
        create_model_provider(
            provider_name="openai_compatible", api_key="sk-test", api_base_url=None,
            logger=logging.getLogger(__name__), safeguards=safeguards,
        )
        assert client.call_args.kwargs["max_retries"] == 0
    with patch("localize.providers.models.create_aisuite_provider") as create:
        create.return_value = SimpleNamespace()
        provider = create_model_provider(
            provider_name="aisuite", api_key="sk-test", api_base_url=None,
            logger=logging.getLogger(__name__), safeguards=safeguards,
            model_names=("gpt-4o-mini",),
            aisuite_provider_configs={"openai": {"max_retries": 99}},
        )
        assert create.call_args.kwargs["provider_configs"]["openai"]["max_retries"] == 0
        assert provider.safeguards is safeguards


@pytest.mark.asyncio
async def test_semantic_clean_result_reused_but_flagged_result_not_cached(tmp_path):
    from unittest.mock import AsyncMock
    from localize.providers.models import OpenAICompatibleProvider
    from localize.semantic_quality import TranslationChange
    from localize.translation_semantic_reviewer import _review_translation_change_batch

    create = AsyncMock(return_value=SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content='{"findings": []}'), finish_reason="stop"),
    ]))
    provider = OpenAICompatibleProvider(client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    provider.safeguards = guard(tmp_path)
    args = dict(
        provider=provider, model="gpt-4o-mini", target_language="German",
        changes=[TranslationChange("app_de.properties", "de", "key", "Hello", "", "Hallo")],
        style_rules=[], brand_glossary=[], reasoning_effort=None,
    )
    assert await _review_translation_change_batch(**args) == []
    assert await _review_translation_change_batch(**args) == []
    assert create.await_count == 1
    args["style_rules"] = ["Use a different register."]
    flagged = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=(
        '{"findings":[{"file":"app_de.properties","key":"key",'
        '"severity":"error","reason":"wrong meaning"}]}'
    )), finish_reason="stop")])
    confirmed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=(
        '{"decisions":[{"id":"finding_0","verdict":"confirmed","reason":"wrong meaning"}]}'
    )), finish_reason="stop")])
    create.side_effect = [flagged, confirmed, flagged, confirmed]
    assert await _review_translation_change_batch(**args)
    assert await _review_translation_change_batch(**args)
    assert create.await_count == 5  # Each flagged result is confirmed exactly once.


@pytest.mark.parametrize(("profile", "limit", "state_path"), [
    ("bisq", 2000, "/app/logs/run_safeguards.sqlite3"),
    ("bisq-mobile", 750, "/app/logs/run_safeguards.sqlite3"),
    # The manual runner replaces this with its workspace store.
    ("bisq1", 1000, "profiles/bisq1/logs/run_safeguards.sqlite3"),
])
def test_packaged_bisq_profiles_enable_documented_limits(profile, limit, state_path):
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    config_path = root / "profiles" / profile / "config.yaml"
    safeguards = load_safeguards(yaml.safe_load(config_path.read_text()), str(config_path))
    assert safeguards is not None
    # Docker keeps the store in the mounted logs directory across restarts.
    assert safeguards.path == root / state_path
    assert safeguards.max_requests_per_24h == limit
    assert (safeguards.sample_files, safeguards.sample_keys,
            safeguards.sample_max_requests) == (3, 5, 60)
    doc = (root / "docs" / "run-safeguards.md").read_text()
    name = {"bisq": "Bisq 2", "bisq-mobile": "Bisq Mobile", "bisq1": "Bisq 1"}[profile]
    assert f"| {name} | {limit:,} |" in doc
