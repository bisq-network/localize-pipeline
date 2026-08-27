import json
import os
import subprocess
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTION = PROJECT_ROOT / "action.yml"


def _run(command, *, cwd, env=None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_action_gate_blocks_seeded_violation_before_publication(tmp_path):
    repo = tmp_path / "target"
    input_folder = repo / "l10n"
    config_folder = repo / "translations"
    report_folder = tmp_path / "reports"
    input_folder.mkdir(parents=True)
    config_folder.mkdir()

    (input_folder / "app.properties").write_text(
        "existing=Existing\nnew.key=Needs translation\n",
        encoding="utf-8",
    )
    target_file = input_folder / "app_de.properties"
    target_file.write_text("existing=Vorhanden\n", encoding="utf-8")
    config_path = config_folder / "config.yaml"
    config_path.write_text(
        """
target_project_root: ".."
input_folder: "l10n"
localization_format: "java_properties"
localization_layout:
  id: "suffix"
  source_locale: "en"
supported_locales:
  - { code: "de", name: "German" }
quality_gate:
  source_identical_min_block_count: 1
  source_identical_max_count: 0
  source_identical_max_ratio: 0.0
""".lstrip(),
        encoding="utf-8",
    )

    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.name", "Action Test"], cwd=repo)
    _run(["git", "config", "user.email", "action@example.invalid"], cwd=repo)
    _run(["git", "config", "commit.gpgSign", "false"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "Seed target repository"], cwd=repo)

    target_file.write_text(
        "existing=Vorhanden\nnew.key=Needs translation\n",
        encoding="utf-8",
    )

    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    gate_step = next(
        step for step in action["runs"]["steps"]
        if step["name"] == "Run translation quality gate"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{PROJECT_ROOT / 'venv' / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(PROJECT_ROOT),
            "TRANSLATOR_CONFIG_FILE": str(config_path),
            "ACTION_QUALITY_REPORT_DIR": str(report_folder),
        }
    )
    result = subprocess.run(
        ["bash", "-c", gate_step["run"]],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    report_path = report_folder / "translation_quality_report.json"
    assert report_path.exists(), (
        f"quality gate produced no report\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    report = json.loads(
        report_path.read_text(encoding="utf-8")
    )
    assert report["blocking"] is True
    assert report["source_identical"]["unexpected_source_identical_count"] == 1
