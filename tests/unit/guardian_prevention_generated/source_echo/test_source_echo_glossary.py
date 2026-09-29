import json
from dataclasses import dataclass
from types import SimpleNamespace

import yaml

from localize.guardian.private_quality import derive_private_findings
from localize.guardian.quality_reports import parse_report
from localize.localization_formats import JAVA_PROPERTIES_FORMAT
from localize.localization_layouts import SUFFIX_LAYOUT
from localize.localization_profiles import LocalizationProfile


def test_changed_source_echoes_across_locales_exclude_exact_glossary_value(tmp_path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    trusted = tmp_path / "trusted"
    for root in (base, head):
        (root / "l10n").mkdir(parents=True)
    trusted.mkdir()

    source = (
        "warning=Keep the receipt for {0} days.\n"
        "notice=Your payment of {0} is ready.\n"
        "learn=Learn more.\n"
    )
    (base / "l10n/messages.properties").write_text(source, encoding="utf-8")
    changed = {}
    path_locales = {}
    for locale, key, old_value, new_value in (
        ("de", "warning", "Beleg {0} Tage aufbewahren.", "Keep the receipt for {0} days."),
        ("fr", "notice", "Votre paiement de {0} est prêt.", "Your payment of {0} is ready."),
    ):
        path = f"l10n/messages_{locale}.properties"
        (base / path).write_text(f"{key}={old_value}\n", encoding="utf-8")
        (head / path).write_text(f"{key}={new_value}\n", encoding="utf-8")
        changed[path] = SimpleNamespace(status="modified")
        path_locales[path] = locale

    pidgin_path = "l10n/messages_pcm.properties"
    (base / pidgin_path).write_text("learn=See more.\n", encoding="utf-8")
    (head / pidgin_path).write_text("learn=Learn more.\n", encoding="utf-8")
    changed[pidgin_path] = SimpleNamespace(status="modified")
    path_locales[pidgin_path] = "pcm"
    (trusted / "glossary.json").write_text(
        json.dumps({"pcm": {"Learn more.": "Learn more."}}), encoding="utf-8"
    )
    config_path = trusted / "config.yaml"
    config_path.write_text(yaml.safe_dump({"input_folder": "l10n"}), encoding="utf-8")

    policy = SimpleNamespace(
        quality_report_actor=SimpleNamespace(login="quality-bot", id=42, type="Bot"),
        allowed_path_globs=("l10n/*.properties",), base_repo="example/app",
    )

    @dataclass(frozen=True)
    class Pull:
        state: str = "open"
        repository: str = "example/app"
        base_repository_id: int = 1
        pull_id: int = 2
        number: int = 3
        head_repository_id: int = 1
        head_sha: str = "a" * 40
        base_sha: str = "b" * 40

    pull = Pull()
    scope = SimpleNamespace(
        config_path=config_path, config_root=trusted,
        changed_files=changed, path_locales=path_locales,
    )

    events = derive_private_findings(
        policy=policy, pull=pull, evidence_head_sha=pull.head_sha,
        head_root=head, base_root=base, scope=scope,
        profiles=(LocalizationProfile(JAVA_PROPERTIES_FORMAT, SUFFIX_LAYOUT),),
        locale_codes=("de", "fr", "pcm"),
    )

    assert {
        (event.locale, finding["key"], finding["category"])
        for event in events
        for finding in parse_report(event.body)["findings"]
    } == {("de", "warning", "source_echo"), ("fr", "notice", "source_echo")}
