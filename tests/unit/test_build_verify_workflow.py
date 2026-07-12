from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-verify.yml"
DEV_REQUIREMENTS = REPO_ROOT / "requirements-dev.txt"
TRIVY_IGNORE = REPO_ROOT / ".trivyignore"
TRIVY_IGNORE_POLICY = REPO_ROOT / ".trivyignore.rego"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_build_verify_runs_on_pull_request_push_and_manual_dispatch():
    workflow = _workflow()

    assert workflow[True]["pull_request"]["branches"] == ["main"]
    assert workflow[True]["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in workflow[True]


def test_build_verify_tests_supported_python_versions():
    job = _workflow()["jobs"]["test-and-package"]

    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    setup = next(step for step in job["steps"] if step["name"].startswith("Set up Python"))
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"


def test_build_verify_builds_and_smoke_tests_wheel():
    steps = _workflow()["jobs"]["test-and-package"]["steps"]
    smoke = next(step for step in steps if step["name"] == "Build and smoke-test package")
    run = smoke["run"]

    assert "python -m build" in run
    assert "python -m pip install --force-reinstall dist/*.whl" in run
    assert "localize --help" in run


def test_build_verify_reports_exact_required_status_context():
    jobs = _workflow()["jobs"]

    assert "test-and-package" in jobs
    assert "build-and-verify" in jobs
    required_context = jobs["build-and-verify"]
    assert required_context.get("name", "build-and-verify") == "build-and-verify"
    assert "strategy" not in required_context
    assert required_context["needs"] == "test-and-package"
    verifier = next(step for step in required_context["steps"] if step["name"] == "Verify matrix result")
    assert "needs.test-and-package.result" in verifier["run"]
    assert "exit 1" in verifier["run"]


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


def test_dev_requirements_pin_setuptools_for_hash_locked_install():
    locked_requirements = DEV_REQUIREMENTS.read_text(encoding="utf-8")

    assert "setuptools==" in locked_requirements
    assert "# setuptools" not in locked_requirements


def test_trivy_vendor_suppressions_are_scoped_and_expire():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    global_ignores = TRIVY_IGNORE.read_text(encoding="utf-8")
    policy = TRIVY_IGNORE_POLICY.read_text(encoding="utf-8")

    assert "--ignore-policy /workspace/.trivyignore.rego" in workflow
    assert "CVE-2026-39831" not in global_ignores
    assert "CVE-2026-39822" not in global_ignores
    assert 'input.VulnerabilityID == "CVE-2026-39831"' in policy
    assert 'input.PkgName == "golang.org/x/crypto"' in policy
    assert 'input.VulnerabilityID == "CVE-2026-39822"' in policy
    assert 'input.PkgName == "stdlib"' in policy
    assert "input.InstalledVersion == affected_os_root_versions[_]" in policy
    assert 'time.parse_rfc3339_ns("2026-08-15T00:00:00Z")' in policy
