"""Exact acceptance controls for externally reviewed source-identical values."""

import pytest
from localize.source_identical_acceptance import normalize_acceptances, is_accepted_source_identical

POLICY = {"pcm": {"chat.private": "Private chat"}}


def test_acceptance_is_exact_locale_key_source_and_target():
    """An unrelated key, locale or later source must not inherit an exemption."""
    assert is_accepted_source_identical("pcm", "chat.private", "Private chat", "Private chat", POLICY)
    for locale, key, source, target in (
        ("de", "chat.private", "Private chat", "Private chat"),
        ("pcm", "other", "Private chat", "Private chat"),
        ("pcm", "chat.private", "Private conversation", "Private conversation"),
        ("pcm", "chat.private", "Private chat", "Privet chat"),
        ("pcm", "chat.private", "Private chat", "private chat"),
    ):
        assert not is_accepted_source_identical(locale, key, source, target, POLICY)


@pytest.mark.parametrize("invalid", [[], {"*": {"chat.private": "Private chat"}},
    {"pcm": []}, {"pcm": {"*": "Private chat"}}, {"pcm": {"chat.private": ""}},
    {"pcm": {"chat.private": 1}}, {"pcm": {"": "Private chat"}}])
def test_malformed_or_broad_acceptances_fail_closed(invalid):
    """Configuration cannot silently create broad or malformed exemptions."""
    with pytest.raises(ValueError):
        normalize_acceptances(invalid)


def test_gate_counts_only_the_exact_accepted_label(tmp_path):
    """Unrelated keys, locales and later source text still count as source echoes."""
    from localize.translation_quality_gate import analyze_source_identical_changes
    folder = tmp_path / "resources"
    folder.mkdir()
    (folder / "app.properties").write_text("chat.private=Private chat\nother=Private chat\n")
    diff = """diff --git a/resources/app_pcm.properties b/resources/app_pcm.properties
+++ b/resources/app_pcm.properties
+chat.private=Private chat
+other=Private chat
"""
    stats = analyze_source_identical_changes(diff, str(tmp_path), str(folder), ["pcm"], [],
                                            accepted_source_identical_translations=POLICY)
    assert stats.expected_source_identical_count == 1
    assert stats.unexpected_source_identical_count == 1
    (folder / "app.properties").write_text("chat.private=Private conversation\nother=Private chat\n")
    stats = analyze_source_identical_changes(diff.replace("chat.private=Private chat", "chat.private=Private conversation"),
        str(tmp_path), str(folder), ["pcm"], [], accepted_source_identical_translations=POLICY)
    assert stats.expected_source_identical_count == 0
    assert stats.unexpected_source_identical_count == 2


def test_gate_config_and_app_config_use_the_same_exact_policy(tmp_path, monkeypatch):
    """Both entry points load validated policy without locale-wide expansion."""
    import yaml
    from localize.app_config import load_app_config
    from localize.translation_quality_gate import load_quality_gate_config
    config_path = tmp_path / "config.yaml"
    payload = {"dry_run": True, "accepted_source_identical_translations": POLICY}
    config_path.write_text(yaml.safe_dump(payload))
    monkeypatch.setenv("TRANSLATOR_CONFIG_FILE", str(config_path))
    assert load_app_config().accepted_source_identical_translations == POLICY
    assert load_quality_gate_config(str(config_path))[0].accepted_source_identical_translations == POLICY
    payload["accepted_source_identical_translations"] = {"pcm": {"*": "Private chat"}}
    config_path.write_text(yaml.safe_dump(payload))
    with pytest.raises(ValueError):
        load_app_config()
    with pytest.raises(ValueError):
        load_quality_gate_config(str(config_path))
