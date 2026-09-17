"""Exercise configured source-identical exemptions through the production gate."""

import json
import subprocess

import pytest
import yaml

from localize.translation_quality_gate import main


@pytest.mark.parametrize("allowlist,locale,value,placeholder_failure,expected", [
    ({"cs": ["Internet"]}, "cs", "Internet", False, 0),
    ({"cs": ["Internet"]}, "de", "Internet", False, 1),
    ({"*": ["Internet"]}, "de", "Internet", False, 0),
    ({"cs": ["Internet"]}, "cs", "Trade", False, 1),
    ({"cs": ["Internet"]}, "cs", "Internet connection", False, 1),
    ({"cs": [" Internet "]}, "cs", "internet", False, 0),
    ({"cs": ["Internet"]}, "cs", "Internet", True, 1),
])
def test_configured_allowlist_through_staged_diff_gate(
    tmp_path, allowlist, locale, value, placeholder_failure, expected,
):
    """Real staged changes retain locale scope and all other validation gates."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "mobile.properties").write_text("message=" + value + "\n")
    target = resources / f"mobile_{locale}.properties"
    target.write_text("")

    def git(*args):
        """Run fixture Git operations without mocking the production diff reader."""
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("add", "resources")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Seed locales")
    target.write_text("message=" + value + "\n")
    git("add", "resources")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "supported_locales": [{"code": locale, "name": locale}],
        "quality_gate": {
            "source_identical_allowlist": allowlist,
            "source_identical_min_block_count": 1,
            "source_identical_max_count": 0,
            "source_identical_max_ratio": 0,
        },
    }))
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({"files": {target.name: {
        "placeholder_failures_count": int(placeholder_failure),
    }}, "pipeline_warnings": []}))
    report_path = tmp_path / "report.json"
    result = main([
        "--repo-root", str(tmp_path), "--input-folder", str(resources),
        "--config", str(config), "--validation-summary", str(validation),
        "--output-json", str(report_path), "--output-markdown", str(tmp_path / "report.md"),
        "--changed-files", f"resources/{target.name}",
    ])
    assert result == expected
    report = json.loads(report_path.read_text())
    assert report["blocking"] is bool(expected)
    assert report["source_identical"]["expected_source_identical_count"] == int(
        expected == 0 or placeholder_failure
    )
