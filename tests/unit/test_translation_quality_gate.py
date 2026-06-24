from pathlib import Path

from src.translation_quality_gate import (
    QualityGateConfig,
    analyze_semantic_qa_changes,
    analyze_source_identical_changes,
    build_quality_gate_report,
    load_quality_gate_config,
    load_validation_summary,
    render_quality_gate_markdown,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_properties(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in entries.items()) + "\n",
        encoding="utf-8",
    )


def test_source_identical_gate_ignores_glossary_tokens_and_enum_keys(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    _write_properties(
        input_folder / "mobile.properties",
        {
            "mobile.open": "Open trades",
            "mobile.sort": "Sort",
            "BTC": "BTC",
            "PAYMENT_METHOD": "MoneyGram",
            "mobile.done": "Done",
        },
    )

    diff_text = """diff --git a/resources/mobile_es.properties b/resources/mobile_es.properties
+++ b/resources/mobile_es.properties
+mobile.open=Open trades
+mobile.sort=Sort
+BTC=BTC
+PAYMENT_METHOD=MoneyGram
+mobile.done=Listo
"""

    stats = analyze_source_identical_changes(
        diff_text=diff_text,
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["es"],
        brand_glossary=["BTC", "MoneyGram"],
    )

    assert stats.changed_entries_count == 5
    assert stats.source_identical_count == 4
    assert stats.expected_source_identical_count == 2
    assert stats.unexpected_source_identical_count == 2
    assert stats.unexpected_source_identical_ratio == 2 / 3


def test_quality_gate_blocks_many_unexpected_source_identical_changes(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    _write_properties(
        input_folder / "mobile.properties",
        {
            "k1": "Open trades",
            "k2": "Sort",
            "k3": "Completed",
            "k4": "Cancelled",
            "k5": "Search",
        },
    )

    diff_text = """diff --git a/resources/mobile_es.properties b/resources/mobile_es.properties
+++ b/resources/mobile_es.properties
+k1=Open trades
+k2=Sort
+k3=Completed
+k4=Cancelled
+k5=Search
"""

    stats = analyze_source_identical_changes(
        diff_text=diff_text,
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["es"],
        brand_glossary=[],
    )
    report = build_quality_gate_report(
        source_stats=stats,
        semantic_stats=None,
        validation_summary={"files": {}, "pipeline_warnings": []},
        changed_files=["resources/mobile_es.properties"],
        input_folder=str(input_folder),
        config=QualityGateConfig(
            source_identical_min_block_count=5,
            source_identical_max_count=20,
            source_identical_max_ratio=0.30,
            block_on_pipeline_warnings=True,
        ),
    )

    assert report["blocking"] is True
    assert "source-identical" in report["blocking_reasons"][0]
    assert report["status_state"] == "failure"


def test_pipeline_warnings_are_blocking_when_configured():
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=".",
            input_folder=".",
            locale_codes=["es"],
            brand_glossary=[],
        ),
        semantic_stats=None,
        validation_summary={
            "files": {},
            "pipeline_warnings": [
                {"file": "mobile_es.properties", "errors": ["Invalid escape sequence"]}
            ],
        },
        changed_files=["resources/mobile_es.properties"],
        input_folder="resources",
        config=QualityGateConfig(block_on_pipeline_warnings=True),
    )

    assert report["blocking"] is True
    assert report["pipeline_warnings_count"] == 1
    assert report["status_state"] == "failure"


def test_pipeline_warnings_are_filtered_to_current_batch():
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=".",
            input_folder="resources",
            locale_codes=["es"],
            brand_glossary=[],
        ),
        semantic_stats=None,
        validation_summary={
            "files": {},
            "pipeline_warnings": [
                {"file": "mobile_es.properties", "errors": ["Current batch warning"]},
                {"file": "mobile_de.properties", "errors": ["Other batch warning"]},
            ],
        },
        changed_files=["resources/mobile_es.properties"],
        input_folder="resources",
        config=QualityGateConfig(block_on_pipeline_warnings=True),
    )

    assert report["blocking"] is True
    assert report["pipeline_warnings_count"] == 1
    assert report["pipeline_warnings"][0]["file"] == "mobile_es.properties"


def test_pipeline_warnings_from_other_batches_do_not_block():
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=".",
            input_folder="resources",
            locale_codes=["es"],
            brand_glossary=[],
        ),
        semantic_stats=None,
        validation_summary={
            "files": {},
            "pipeline_warnings": [
                {"file": "mobile_de.properties", "errors": ["Other batch warning"]},
            ],
        },
        changed_files=["resources/mobile_es.properties"],
        input_folder="resources",
        config=QualityGateConfig(block_on_pipeline_warnings=True),
    )

    assert report["blocking"] is False
    assert report["pipeline_warnings_count"] == 0


def test_missing_validation_summary_loads_empty_summary(tmp_path):
    summary = load_validation_summary(str(tmp_path / "missing.json"))

    assert summary == {"files": {}, "pipeline_warnings": []}


def test_semantic_qa_blocks_recent_coderabbit_translation_nits(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    input_folder.mkdir()
    _, _, _, semantic_rules = load_quality_gate_config(str(PROJECT_ROOT / "docker" / "config.docker.yaml"))
    diff_text = """diff --git a/resources/mobile_af_ZA.properties b/resources/mobile_af_ZA.properties
+++ b/resources/mobile_af_ZA.properties
+mobile.tradeHistory.count.many={0} handelaars
+mobile.tradeHistory.count.filtered={0} van {1} handelaars
diff --git a/resources/mobile_es.properties b/resources/mobile_es.properties
+++ b/resources/mobile_es.properties
+mobile.tradeHistory.details.tradersAndRole=Traders / Rol
diff --git a/resources/mobile_fr.properties b/resources/mobile_fr.properties
+++ b/resources/mobile_fr.properties
+mobile.tradeHistory.details.tradersAndRole=Traders / Rôle
diff --git a/resources/mobile_vi.properties b/resources/mobile_vi.properties
+++ b/resources/mobile_vi.properties
+mobile.tradeHistory.details.networkAddress.clear=Xóa địa chỉ mạng: {0}
"""

    semantic_stats = analyze_semantic_qa_changes(
        diff_text=diff_text,
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["af_ZA", "es", "fr", "vi"],
        semantic_rules=semantic_rules,
    )
    report = build_quality_gate_report(
        source_stats=analyze_source_identical_changes(
            diff_text="",
            repo_root=str(repo_root),
            input_folder=str(input_folder),
            locale_codes=["af_ZA", "es", "fr", "vi"],
            brand_glossary=[],
        ),
        semantic_stats=semantic_stats,
        validation_summary={"files": {}, "pipeline_warnings": []},
        changed_files=[
            "resources/mobile_af_ZA.properties",
            "resources/mobile_es.properties",
            "resources/mobile_fr.properties",
            "resources/mobile_vi.properties",
        ],
        input_folder=str(input_folder),
        config=QualityGateConfig(block_on_semantic_qa_findings=True),
    )

    assert semantic_stats.findings_count == 5
    assert semantic_stats.errors_count == 5
    assert report["blocking"] is True
    assert "Semantic translation QA" in report["blocking_reasons"][0]


def test_semantic_qa_reports_retained_source_words_but_ignores_glossary(tmp_path):
    repo_root = tmp_path
    input_folder = repo_root / "resources"
    _write_properties(
        input_folder / "mobile.properties",
        {
            "mobile.some.label": "Settlement Details",
            "mobile.invoice": "Lightning invoice",
        },
    )
    diff_text = """diff --git a/resources/mobile_es.properties b/resources/mobile_es.properties
+++ b/resources/mobile_es.properties
+mobile.some.label=Detalles Settlement
+mobile.invoice=Factura Lightning
"""

    semantic_stats = analyze_semantic_qa_changes(
        diff_text=diff_text,
        repo_root=str(repo_root),
        input_folder=str(input_folder),
        locale_codes=["es"],
        brand_glossary=["Lightning"],
    )

    assert semantic_stats.findings_count == 1
    assert "Settlement" in semantic_stats.examples[0]["reason"]


def test_quality_gate_markdown_contains_validation_summary():
    report = {
        "blocking": False,
        "source_identical": {
            "changed_entries_count": 10,
            "checked_entries_count": 8,
            "source_identical_count": 3,
            "expected_source_identical_count": 2,
            "unexpected_source_identical_count": 1,
            "unexpected_source_identical_ratio": 0.125,
            "examples": [],
        },
        "semantic_qa": {
            "findings_count": 1,
            "examples": [
                {
                    "file": "mobile_es.properties",
                    "key": "mobile.tradeHistory.details.tradersAndRole",
                    "value": "Traders / Rol",
                    "reason": "Trade Details traders/role label contains untranslated English term 'Traders'.",
                }
            ],
        },
        "validation": {
            "reverted_keys_count": 2,
            "control_character_findings_count": 1,
            "placeholder_failures_count": 1,
        },
        "pipeline_warnings_count": 0,
        "pipeline_warnings": [],
        "blocking_reasons": [],
    }

    markdown = render_quality_gate_markdown(report)

    assert "Translation Validation Summary" in markdown
    assert "Unexpected source-identical changed values" in markdown
    assert "Semantic QA findings" in markdown
    assert "Semantic QA Examples" in markdown
    assert "Reverted keys" in markdown
    assert "Control-character findings" in markdown
