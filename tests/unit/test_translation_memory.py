import json

from localize.translation_memory import TranslationMemory, load_translation_memory, save_translation_memory
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


def test_save_translation_memory_is_best_effort(monkeypatch, tmp_path):
    memory = TranslationMemory()
    memory.record("Save", "Speichern", locale="de", format_id="json")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", fail_write)

    save_translation_memory(tmp_path / "translation_memory.json", memory)


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
