"""Keep operator documentation aligned with the deployed shell and Compose paths."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    """Read one repository file as UTF-8 text."""
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _markdown_section(document: str, heading: str) -> str:
    """Return one level-two Markdown section without depending on its prose."""
    match = re.search(
        rf"^{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        document,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing documentation section: {heading}"
    return match.group("body")


def test_environment_reference_tracks_the_runtime_pr_batch_default():
    """Keep the documented PR batch default equal to the shell default."""
    script = _read("update-translations.sh")
    reference = _read("docs/environment-variables.md")

    runtime_match = re.search(
        r"^DEFAULT_MAX_FILES_PER_PR=(\d+)$", script, flags=re.MULTILINE
    )
    reference_match = re.search(
        r"^\| `MAX_FILES_PER_PR` \|.*?defaults to `(\d+)`\. \|$",
        reference,
        flags=re.MULTILINE,
    )

    assert runtime_match is not None
    assert reference_match is not None
    assert reference_match.group(1) == runtime_match.group(1)


def test_server_guide_describes_compose_keys_as_runtime_secrets():
    """Pin the documented and deployed Compose runtime-secret contract."""
    guide = _read("docs/new-project-deployment.md")
    compose = yaml.safe_load(_read("docker/docker-compose.yml"))
    translator = compose["services"]["translator"]

    assert translator["secrets"] == [
        {
            "source": "gpg_bot_key",
            "target": "gpg_bot_key",
            "mode": 0o400,
        },
        {
            "source": "deploy_key",
            "target": "deploy_key",
            "mode": 0o400,
        },
    ]
    assert compose["secrets"] == {
        "gpg_bot_key": {
            "file": "${GPG_BOT_KEY_FILE:-../secrets/gpg_bot_key/bot_secret_key.asc}"
        },
        "deploy_key": {
            "file": "${DEPLOY_KEY_FILE:-../secrets/deploy_key/id_ed25519}"
        },
    }
    assert "secrets" not in translator.get("build", {})
    assert "runtime secret" in guide.lower()
    assert "build secret" not in guide.lower()
    assert "/run/secrets/" in guide


def test_server_guide_smokes_the_cli_without_running_the_repository_entrypoint():
    """Require the server smoke check to bypass the production entrypoint."""
    guide = _read("docs/new-project-deployment.md")

    assert "--no-deps --entrypoint python3.11 translator" in guide
    assert "-m localize.cli formats" in guide
    assert "format/config check" not in guide


def test_local_docker_guide_uses_the_root_safe_compose_command_and_runtime_keys():
    """Keep local commands rooted correctly and runtime-secret-aware."""
    guide = _read("docs/how-to-run-locally.md")
    canonical_compose_prefix = (
        "docker compose --env-file docker/.env -f docker/docker-compose.yml"
    )

    assert f"{canonical_compose_prefix} build" in guide
    assert f"{canonical_compose_prefix} run -T --rm translator" in guide
    assert "runtime secret" in guide.lower()
    assert "baked-in" not in guide.lower()


def test_cron_template_uses_the_same_explicit_root_compose_command():
    """Keep the cron template aligned with the root-level Compose command."""
    cron = _read("docker/translator-cron")
    canonical_run = (
        "/usr/bin/docker compose --env-file docker/.env "
        "-f docker/docker-compose.yml run -T --rm translator"
    )

    assert "cd /path/to/your/project && mkdir -p logs &&" in cron
    assert canonical_run in cron
    assert "cd /path/to/your/project/docker" not in cron


def test_profile_edits_do_not_require_an_image_rebuild():
    """Document that bind-mounted profile edits take effect without a rebuild."""
    guide = _read("docs/new-project-deployment.md")
    checks = _markdown_section(guide, "## Operational Checks")
    rebuild_bullet = next(
        line for line in checks.splitlines() if line.startswith("- Rebuild after")
    )

    assert "profile" not in rebuild_bullet.lower()
    assert "without rebuilding the image" in checks


def test_security_strategy_matches_host_cron_and_runtime_secret_boundaries():
    """Keep the security guide aligned with the deployed runtime boundaries."""
    strategy = _read("SECURITY_STRATEGY.md")
    server = _markdown_section(strategy, "## 1. Server Deployment Security")
    local = _markdown_section(strategy, "## 2. Local Development Security")

    assert "host-level cron" in server.lower()
    assert "runtime secret" in server.lower()
    assert "/etc/environment" not in strategy
    assert "All secrets" not in strategy
    assert "Private deploy and signing keys remain separate host files" in strategy
    assert "docker-compose.override" not in local
    assert "**SSH Agent Forwarding**" not in local
    assert "runtime secret" in local.lower()
