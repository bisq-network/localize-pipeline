import json

from localize.localization_profiles import load_localization_profiles
from localize.semantic_remediation import apply_semantic_review_suggestions


def test_semantic_remediation_applies_properties_suggestion_with_placeholder_parity(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo {0}",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
    )

    assert result.applied_count == 1
    assert result.skipped_count == 0
    assert "hello=Hallo {0}" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_skips_suggestions_that_break_placeholders(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
    )

    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert "hello=Hallo\n" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_applies_json_locale_directory_suggestion(tmp_path):
    resources = tmp_path / "resources"
    source_dir = resources / "en"
    target_dir = resources / "de"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    (source_dir / "common.json").write_text(
        json.dumps({"hello": "Hello {0}"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    target_path = target_dir / "common.json"
    target_path.write_text(
        json.dumps({"hello": "Hallo"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "de/common.json",
                "key": "/hello",
                "severity": "error",
                "suggested_value": "Hallo {0}",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({
            "localization_formats": [
                {
                    "id": "json",
                    "layout": {"id": "locale_directory", "source_locale": "en"},
                }
            ]
        }),
    )

    assert result.applied_count == 1
    assert json.loads(target_path.read_text(encoding="utf-8"))["hello"] == "Hallo {0}"
