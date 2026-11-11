"""Keep accepted outputs bound to the configured source-passthrough policy."""

import pytest

from localize.run_safeguards import load_safeguards


LEGAL_BODY_PATTERN = r"^tacWindow\.legal\.section[1-7]\.body$"


@pytest.mark.parametrize(
    ("old_patterns", "new_patterns"),
    [([], [LEGAL_BODY_PATTERN]), ([LEGAL_BODY_PATTERN], [])],
    ids=["add-source-passthrough", "remove-source-passthrough"],
)
def test_ignore_policy_change_invalidates_persisted_outputs(
    tmp_path, old_patterns, new_patterns,
):
    # The ignored key was not selected in the old run, so source, target and
    # selected-key inputs can stay identical when only the policy changes.
    inputs = {
        "file": "displayStrings_de.properties",
        "source": {"tacWindow.legal.section1.body": "Legal text", "other": "New"},
        "target": {"tacWindow.legal.section1.body": "Rechtlicher Text", "other": ""},
        "keys": ["other"],
    }
    config = {
        "run_safeguards": {"state_path": str(tmp_path / "state.sqlite3")},
        "ignore_key_patterns": old_patterns,
    }
    config_path = tmp_path / "config.yaml"
    old = load_safeguards(config, config_path)
    old_key = old.fingerprint(inputs)
    legal_value = "Legal text" if old_patterns else "Rechtlicher Text"
    accepted = {"content": f"tacWindow.legal.section1.body={legal_value}\nother=Neu\n"}
    candidates = {"other": "Neu"}
    old.accept(old_key, accepted)
    old.save_candidates(old_key, candidates)

    restarted = load_safeguards(config, config_path)
    assert restarted.lookup(restarted.fingerprint(inputs)) == accepted
    assert restarted.lookup_candidates(restarted.fingerprint(inputs)) == candidates

    changed = load_safeguards(
        {**config, "ignore_key_patterns": new_patterns}, config_path,
    )
    new_key = changed.fingerprint(inputs)
    assert new_key != old_key
    assert changed.lookup(new_key) is None
    assert changed.lookup_candidates(new_key) == {}
    # Policy invalidation does not erase retained outputs or charge requests.
    assert changed.lookup(old_key) == accepted
    assert changed.request_count() == 0
