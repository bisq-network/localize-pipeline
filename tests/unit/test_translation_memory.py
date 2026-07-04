import json
import os

os.environ.setdefault("OPENAI_API_KEY", "DUMMY_KEY_FOR_TESTING")

from localize.translation_memory import (
    TranslationMemory,
    load_translation_memory_strict,
    load_translation_memory,
    memory_source_hash,
    merge_translation_memory,
    save_translation_memory,
    translation_memory_suggestions,
)
from localize.translate_localization_files import apply_translation_memory


def test_translation_memory_roundtrip_and_lookup(tmp_path):
    memory = TranslationMemory()
    memory.record(
        source_text="Save changes",
        target_text="Änderungen speichern",
        locale="de",
        format_id="json",
    )
    path = tmp_path / "translation_memory.json"

    save_translation_memory(path, memory)
    loaded = load_translation_memory(path)

    assert loaded.lookup("Save changes", locale="de", format_id="json") == "Änderungen speichern"
    assert loaded.lookup("Save changes", locale="fr", format_id="json") is None


def test_translation_memory_marks_conflicts_and_stops_reusing_ambiguous_segments():
    memory = TranslationMemory()
    memory.record("Open", "Öffnen", locale="de", format_id="java_properties")
    memory.record("Open", "Offen", locale="de", format_id="java_properties")

    assert memory.lookup("Open", locale="de", format_id="java_properties") is None
    payload = memory.to_payload()
    entry = next(iter(payload["entries"].values()))
    assert entry["status"] == "conflict"
    assert sorted(entry["targets"]) == ["Offen", "Öffnen"]


def test_translation_memory_demotes_single_target_conflicts():
    memory = TranslationMemory.from_payload({
        "entries": {
            f"java_properties:de:{memory_source_hash('Open')}": {
                "source": "Open",
                "source_hash": memory_source_hash("Open"),
                "locale": "de",
                "format_id": "java_properties",
                "status": "conflict",
                "targets": ["Öffnen"],
            }
        }
    })

    memory.record("Open", "Öffnen", locale="de", format_id="java_properties")

    assert memory.lookup("Open", locale="de", format_id="java_properties") == "Öffnen"
    entry = next(iter(memory.to_payload()["entries"].values()))
    assert entry["status"] == "active"
    assert entry["target"] == "Öffnen"
    assert "targets" not in entry


def test_translation_memory_preserves_spacing_in_exact_match_keys():
    memory = TranslationMemory()
    memory.record("Open  trades", "Offene  Trades", locale="de", format_id="json")

    assert memory.lookup("Open  trades", locale="de", format_id="json") == "Offene  Trades"
    assert memory.lookup("Open trades", locale="de", format_id="json") is None


def test_load_translation_memory_missing_or_invalid_file_is_empty(tmp_path):
    assert load_translation_memory(tmp_path / "missing.json").to_payload()["entries"] == {}

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"entries": []}), encoding="utf-8")

    assert load_translation_memory(invalid).to_payload()["entries"] == {}


def test_strict_load_translation_memory_rejects_missing_and_invalid_files(tmp_path):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")

    try:
        load_translation_memory_strict(missing, require_exists=True)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing strict memory file should fail")

    try:
        load_translation_memory_strict(invalid)
    except ValueError as exc:
        assert "invalid translation memory" in str(exc)
    else:
        raise AssertionError("invalid strict memory file should fail")


def test_save_translation_memory_is_best_effort(monkeypatch, tmp_path):
    memory = TranslationMemory()
    memory.record("Save", "Speichern", locale="de", format_id="json")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", fail_write)

    save_translation_memory(tmp_path / "translation_memory.json", memory)


def test_save_translation_memory_catches_non_os_errors(monkeypatch, tmp_path):
    memory = TranslationMemory()
    memory.record("Save", "Speichern", locale="de", format_id="json")

    def fail_write(*_args, **_kwargs):
        raise RuntimeError("json encoder failed")

    monkeypatch.setattr("localize.translation_memory.write_translation_memory", fail_write)

    save_translation_memory(tmp_path / "translation_memory.json", memory)


def test_translation_memory_stats_counts_active_conflict_locale_and_format():
    memory = TranslationMemory()
    memory.record("Save", "Speichern", locale="de", format_id="json")
    memory.record("Open", "Öffnen", locale="de", format_id="java_properties")
    memory.record("Open", "Offen", locale="de", format_id="java_properties")

    stats = memory.stats()

    assert stats.total_entries == 2
    assert stats.active_entries == 1
    assert stats.conflict_entries == 1
    assert stats.locales == ("de",)
    assert stats.formats == ("java_properties", "json")


def test_merge_translation_memory_imports_entries_and_marks_conflicts():
    base = TranslationMemory()
    incoming = TranslationMemory()
    base.record("Save", "Speichern", locale="de", format_id="json")
    incoming.record("Cancel", "Abbrechen", locale="de", format_id="json")
    incoming.record("Save", "Sichern", locale="de", format_id="json")

    result = merge_translation_memory(base, incoming)

    assert result.imported_entries == 1
    assert result.conflict_entries == 1
    assert base.lookup("Cancel", locale="de", format_id="json") == "Abbrechen"
    assert base.lookup("Save", locale="de", format_id="json") is None


def test_merge_translation_memory_demotes_single_target_conflicts():
    base = TranslationMemory.from_payload({
        "entries": {
            f"json:de:{memory_source_hash('Save')}": {
                "source": "Save",
                "source_hash": memory_source_hash("Save"),
                "locale": "de",
                "format_id": "json",
                "status": "conflict",
                "targets": ["Speichern"],
            }
        }
    })
    incoming = TranslationMemory()
    incoming.record("Save", "Speichern", locale="de", format_id="json")

    result = merge_translation_memory(base, incoming)

    assert result.unchanged_entries == 1
    assert base.lookup("Save", locale="de", format_id="json") == "Speichern"
    entry = next(iter(base.to_payload()["entries"].values()))
    assert entry["status"] == "active"
    assert entry["target"] == "Speichern"
    assert "targets" not in entry


def test_translation_memory_suggestions_are_fuzzy_but_not_automatic_reuse():
    memory = TranslationMemory()
    memory.record("Save changes", "Änderungen speichern", locale="de", format_id="json")
    memory.record("Delete account", "Konto löschen", locale="de", format_id="json")

    suggestions = translation_memory_suggestions(
        memory,
        "Save change",
        locale="de",
        format_id="json",
        min_score=0.75,
    )

    assert suggestions[0].source_text == "Save changes"
    assert suggestions[0].target_text == "Änderungen speichern"
    assert suggestions[0].score < 1.0
    assert memory.lookup("Save change", locale="de", format_id="json") is None


def test_translation_memory_suggestions_include_legacy_exact_hash_entries():
    legacy = TranslationMemory.from_payload({
        "entries": {
            "json:de:legacy": {
                "source_hash": memory_source_hash("Save changes"),
                "locale": "de",
                "format_id": "json",
                "target": "Änderungen speichern",
                "status": "active",
            }
        }
    })

    suggestions = translation_memory_suggestions(
        legacy,
        "Save changes",
        locale="de",
        format_id="json",
        min_score=1.0,
    )

    assert suggestions[0].source_text == "Save changes"
    assert suggestions[0].target_text == "Änderungen speichern"
    assert suggestions[0].score == 1.0


def test_apply_translation_memory_splits_hits_from_model_work():
    memory = TranslationMemory()
    memory.record("Save", "Speichern", locale="de", format_id="json")

    plan = apply_translation_memory(
        texts_to_translate=["Save", "Cancel"],
        indices=[3, 4],
        keys_to_translate=["button.save", "button.cancel"],
        memory=memory,
        locale="de",
        format_id="json",
    )

    assert plan.cached_results == [(0, "Speichern")]
    assert plan.pending_texts == ["Cancel"]
    assert plan.pending_line_indices == [4]
    assert plan.pending_keys == ["button.cancel"]
    assert plan.pending_positions == [1]
