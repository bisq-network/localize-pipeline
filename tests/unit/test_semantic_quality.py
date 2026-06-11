from pathlib import Path

from src.semantic_quality import (
    SemanticRule,
    TranslationChange,
    analyze_all_translation_entries,
    evaluate_retained_source_words,
    evaluate_semantic_rules,
    load_semantic_rules,
)
from src.translation_quality_gate import (
    QualityGateConfig,
    analyze_semantic_qa_changes,
    analyze_source_identical_changes,
    build_quality_gate_report,
)


def _write_properties(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in entries.items()) + "\n",
        encoding="utf-8",
    )


def test_declarative_semantic_rule_blocks_forbidden_target_text(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    input_folder.mkdir()
    rules = [
        SemanticRule(
            id="trade-history-traders-label",
            message="Fully localize the traders label.",
            locales=("es", "fr"),
            keys=("mobile.tradeHistory.details.tradersAndRole",),
            forbidden_target_regex=r"\bTraders\b",
            severity="error",
        )
    ]
    diff_text = """diff --git a/resources/mobile_es.properties b/resources/mobile_es.properties
+++ b/resources/mobile_es.properties
+mobile.tradeHistory.details.tradersAndRole=Traders / Rol
"""

    semantic_stats = analyze_semantic_qa_changes(
        diff_text=diff_text,
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["es"],
        semantic_rules=rules,
    )
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=str(repo_root),
            input_folder=str(input_folder),
            locale_codes=["es"],
            brand_glossary=[],
        ),
        semantic_stats=semantic_stats,
        validation_summary={"files": {}, "pipeline_warnings": []},
        changed_files=["resources/mobile_es.properties"],
        input_folder=str(input_folder),
        config=QualityGateConfig(block_on_semantic_qa_findings=True),
    )

    assert semantic_stats.errors_count == 1
    assert semantic_stats.warnings_count == 0
    assert semantic_stats.examples[0]["rule_id"] == "trade-history-traders-label"
    assert report["blocking"] is True


def test_retained_source_word_findings_are_warning_only_by_default():
    change = TranslationChange(
        file="resources/mobile_es.properties",
        locale_code="es",
        key="mobile.some.label",
        source_value="Settlement Details",
        old_value=None,
        new_value="Detalles Settlement",
    )

    findings = evaluate_retained_source_words(
        changes=[change],
        brand_glossary=["Lightning"],
    )

    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].rule_id == "retained-source-word"


def test_semantic_warnings_do_not_block_unless_configured(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    _write_properties(input_folder / "mobile.properties", {"k": "Settlement Details"})
    diff_text = """diff --git a/resources/mobile_es.properties b/resources/mobile_es.properties
+++ b/resources/mobile_es.properties
+k=Detalles Settlement
"""

    semantic_stats = analyze_semantic_qa_changes(
        diff_text=diff_text,
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["es"],
        brand_glossary=[],
    )
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=str(repo_root),
            input_folder=str(input_folder),
            locale_codes=["es"],
            brand_glossary=[],
        ),
        semantic_stats=semantic_stats,
        validation_summary={"files": {}, "pipeline_warnings": []},
        changed_files=["resources/mobile_es.properties"],
        input_folder=str(input_folder),
        config=QualityGateConfig(
            block_on_semantic_qa_findings=True,
            block_on_semantic_qa_warnings=False,
        ),
    )

    assert semantic_stats.findings_count == 1
    assert semantic_stats.warnings_count == 1
    assert report["blocking"] is False


def test_ai_review_error_findings_are_folded_into_quality_gate(tmp_path):
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=str(tmp_path),
            input_folder=str(tmp_path),
            locale_codes=["es"],
            brand_glossary=[],
        ),
        semantic_stats=None,
        validation_summary={
            "files": {},
            "pipeline_warnings": [],
            "semantic_review_findings": [
                {
                    "file": "mobile_es.properties",
                    "key": "mobile.clear",
                    "severity": "error",
                    "reason": "The target uses a delete verb for a clearnet label.",
                    "value": "Borrar red: {0}",
                    "source": "ai-review",
                }
            ],
        },
        changed_files=["resources/mobile_es.properties"],
        input_folder="resources",
        config=QualityGateConfig(block_on_semantic_qa_findings=True),
    )

    assert report["semantic_qa"]["errors_count"] == 1
    assert report["blocking"] is True
    assert "Semantic translation QA" in report["blocking_reasons"][0]


def test_full_semantic_audit_scans_entries_without_diff(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    _write_properties(input_folder / "mobile.properties", {"mobile.some.label": "Settlement Details"})
    _write_properties(input_folder / "mobile_es.properties", {"mobile.some.label": "Detalles Settlement"})

    stats = analyze_all_translation_entries(
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["es"],
        brand_glossary=[],
        semantic_rules=[],
    )

    assert stats.findings_count == 1
    assert stats.warnings_count == 1
    assert stats.examples[0]["file"] == "mobile_es.properties"


def test_feedback_rules_load_review_source_metadata():
    rules = load_semantic_rules(
        [
            {
                "id": "vi-clearnet-clear",
                "message": "Do not translate clear as delete.",
                "locales": ["vi"],
                "keys": ["mobile.tradeHistory.details.networkAddress.clear"],
                "forbidden_target_regex": r"^Xóa\b",
                "severity": "error",
                "source": "bisq-mobile#1478 CodeRabbit",
            }
        ]
    )

    findings = evaluate_semantic_rules(
        changes=[
            TranslationChange(
                file="resources/mobile_vi.properties",
                locale_code="vi",
                key="mobile.tradeHistory.details.networkAddress.clear",
                source_value="Clear network address: {0}",
                old_value=None,
                new_value="Xóa địa chỉ mạng: {0}",
            )
        ],
        rules=rules,
    )

    assert findings[0].rule_id == "vi-clearnet-clear"
    assert findings[0].source == "bisq-mobile#1478 CodeRabbit"
