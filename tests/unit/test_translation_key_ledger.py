import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

from localize.translate_localization_files import (
    build_file_key_ledger,
    compute_ledger_hash,
    load_translation_key_ledger,
    save_translation_key_ledger
)


def test_load_translation_key_ledger_missing_file():
    with tempfile.TemporaryDirectory() as temp_dir:
        ledger_path = Path(temp_dir) / "missing-ledger.json"
        loaded = load_translation_key_ledger(str(ledger_path))
        assert loaded == {}


def test_translation_key_ledger_roundtrip():
    with tempfile.TemporaryDirectory() as temp_dir:
        ledger_path = Path(temp_dir) / "ledger.json"
        key_ledger = {
            "mobile_de.properties": {
                "key.one": {
                    "source_hash": compute_ledger_hash("Source one"),
                    "target_hash": compute_ledger_hash("Ziel eins")
                }
            }
        }

        save_translation_key_ledger(str(ledger_path), key_ledger)
        loaded = load_translation_key_ledger(str(ledger_path))

        assert loaded == key_ledger


def test_load_translation_key_ledger_corrupt_file_fails_closed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="LOCALIZE_ALLOW_RESET_LEDGER"):
        load_translation_key_ledger(str(ledger_path))

    assert not ledger_path.exists()
    assert list(tmp_path.glob("ledger.json.corrupt-*"))


def test_load_translation_key_ledger_corrupt_file_can_be_reset(monkeypatch, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LOCALIZE_ALLOW_RESET_LEDGER", "true")

    assert load_translation_key_ledger(str(ledger_path)) == {}

    assert not ledger_path.exists()
    assert list(tmp_path.glob("ledger.json.corrupt-*"))


def test_load_translation_key_ledger_reports_failed_backup_when_reset_allowed(
        monkeypatch, tmp_path, caplog
):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LOCALIZE_ALLOW_RESET_LEDGER", "true")

    def fail_replace(_source, _target):
        raise OSError("disk full")

    monkeypatch.setattr("localize.translate_localization_files.os.replace", fail_replace)

    with caplog.at_level(logging.WARNING):
        assert load_translation_key_ledger(str(ledger_path)) == {}

    log_output = caplog.text
    assert "could not be backed up" in log_output
    assert "was backed up" not in log_output
    assert ledger_path.exists()


def test_save_translation_key_ledger_uses_atomic_json_writer(monkeypatch, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    key_ledger = {
        "mobile_de.properties": {
            "key.one": {
                "source_hash": compute_ledger_hash("Source one"),
                "target_hash": compute_ledger_hash("Ziel eins")
            }
        }
    }
    calls = []

    def fake_write_json_atomic(path, payload, **kwargs):
        calls.append((path, payload, kwargs))

    monkeypatch.setattr(
        "localize.translate_localization_files.write_json_atomic",
        fake_write_json_atomic,
    )

    save_translation_key_ledger(str(ledger_path), key_ledger)

    assert len(calls) == 1
    path, payload, kwargs = calls[0]
    assert path == str(ledger_path)
    assert payload["version"] == 1
    assert payload["files"] == key_ledger
    assert payload["updated_at"].endswith("Z")
    assert kwargs == {"sort_keys": True}


def test_translation_key_ledger_timestamp_uses_utc_z_suffix():
    with tempfile.TemporaryDirectory() as temp_dir:
        ledger_path = Path(temp_dir) / "ledger.json"
        key_ledger = {
            "mobile_de.properties": {
                "key.one": {
                    "source_hash": compute_ledger_hash("Source one"),
                    "target_hash": compute_ledger_hash("Ziel eins")
                }
            }
        }

        save_translation_key_ledger(str(ledger_path), key_ledger)
        with open(ledger_path, "r", encoding="utf-8") as ledger_file:
            payload = json.load(ledger_file)

        assert payload["updated_at"].endswith("Z")


def test_save_translation_key_ledger_with_filename_only_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            key_ledger = {
                "mobile_de.properties": {
                    "key.one": {
                        "source_hash": compute_ledger_hash("Source one"),
                        "target_hash": compute_ledger_hash("Ziel eins")
                    }
                }
            }

            save_translation_key_ledger("ledger.json", key_ledger)
            loaded = load_translation_key_ledger("ledger.json")
            assert loaded == key_ledger
        finally:
            os.chdir(old_cwd)


def test_build_file_key_ledger_produces_correct_hashes():
    source_translations = {
        "key.keep": "Source Keep",
        "key.new": "Source New"
    }
    final_translations = {
        "key.keep": "Ziel Keep",
        "key.new": "Ziel New"
    }

    built = build_file_key_ledger(source_translations, final_translations)

    assert set(built.keys()) == {"key.keep", "key.new"}
    assert built["key.keep"]["source_hash"] == compute_ledger_hash("Source Keep")
    assert built["key.keep"]["target_hash"] == compute_ledger_hash("Ziel Keep")


def test_build_file_key_ledger_marks_failed_keys():
    source_translations = {"key.one": "Source One"}
    final_translations = {"key.one": "Source One"}

    built = build_file_key_ledger(
        source_translations,
        final_translations,
        failed_keys={"key.one"}
    )

    assert built["key.one"]["status"] == "failed"
