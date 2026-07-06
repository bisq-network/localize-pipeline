from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-verify.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_build_verify_runs_on_pull_request_push_and_manual_dispatch():
    workflow = _workflow()

    assert workflow[True]["pull_request"]["branches"] == ["main"]
    assert workflow[True]["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in workflow[True]


def test_build_verify_tests_supported_python_versions():
    job = _workflow()["jobs"]["build-and-verify"]

    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    setup = next(step for step in job["steps"] if step["name"].startswith("Set up Python"))
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"


def test_build_verify_builds_and_smoke_tests_wheel():
    steps = _workflow()["jobs"]["build-and-verify"]["steps"]
    smoke = next(step for step in steps if step["name"] == "Build and smoke-test package")
    run = smoke["run"]

    assert "python -m build" in run
    assert "python -m pip install --force-reinstall dist/*.whl" in run
    assert "localize --help" in run


def test_build_verify_uses_sha_pinned_actions():
    rendered = WORKFLOW.read_text(encoding="utf-8")

    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in rendered
    assert "hadolint/hadolint-action@54c9adbab1582c2ef04b2016b760714a4bfde3cf" in rendered
    assert "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9" in rendered
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in rendered
    assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in rendered
    assert "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a" in rendered
    active_uses_lines = [
        line.strip()
        for line in rendered.splitlines()
        if line.lstrip().startswith("uses:")
    ]
    assert not any("@v" in line for line in active_uses_lines)
