import json

from localize.atomic_io import write_json_atomic, write_text_atomic


def test_write_text_atomic_creates_parent_and_replaces_file(tmp_path):
    path = tmp_path / "reports" / "summary.txt"

    write_text_atomic(path, "first")
    write_text_atomic(path, "second")

    assert path.read_text(encoding="utf-8") == "second"
    assert not list(path.parent.glob(".summary.txt.*.tmp"))


def test_write_json_atomic_uses_stable_pretty_json(tmp_path):
    path = tmp_path / "summary.json"

    write_json_atomic(path, {"b": 2, "a": "ä"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "ä", "b": 2}
    assert path.read_text(encoding="utf-8").endswith("\n")
