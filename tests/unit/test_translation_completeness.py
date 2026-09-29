import json
from unittest.mock import patch

from localize.translation_quality_gate import main


def test_quality_gate_flags_unchanged_source_prose_in_changed_locale_file(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    source = {
        "muSig.offer.taker.review.sendTakeOfferMessageFeedback.headline": "Send the take offer message",
        "muSig.offer.taker.review.sendTakeOfferMessageFeedback.subTitle": "Waiting for the peer to respond",
        "action": "Continue",
        "brand": "Bisq",
        "placeholder": "{0}",
        "shared": "OK",
    }
    target = {**source, "action": "Halda áfram"}
    for name, entries in (("messages.properties", source), ("messages_is.properties", target)):
        (resources / name).write_text(
            "".join(f"{key}={value}\n" for key, value in entries.items()),
            encoding="utf-8",
        )
    config = tmp_path / "config.yaml"
    config.write_text(
        "supported_locales:\n  - code: is\nbrand_technical_glossary:\n  - Bisq\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    diff = """diff --git a/resources/messages_is.properties b/resources/messages_is.properties
--- a/resources/messages_is.properties
+++ b/resources/messages_is.properties
@@ -3 +3 @@
-action=Continue
+action=Halda áfram
"""

    with patch("localize.translation_quality_gate.get_staged_diff", return_value=diff):
        result = main([
            "--repo-root", str(tmp_path),
            "--input-folder", str(resources),
            "--config", str(config),
            "--validation-summary", str(tmp_path / "missing-summary.json"),
            "--output-json", str(report_path),
            "--output-markdown", str(tmp_path / "report.md"),
            "--changed-files", "resources/messages_is.properties",
        ])

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert report["blocking"] is True
    assert {item["key"] for item in report["semantic_qa"]["examples"]} == {
        "muSig.offer.taker.review.sendTakeOfferMessageFeedback.headline",
        "muSig.offer.taker.review.sendTakeOfferMessageFeedback.subTitle",
    }
