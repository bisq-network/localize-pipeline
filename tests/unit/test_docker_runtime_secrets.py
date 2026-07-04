"""Static checks for Docker runtime secret handling."""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile"
COMPOSE_FILE = PROJECT_ROOT / "docker" / "docker-compose.yml"
ENTRYPOINT = PROJECT_ROOT / "docker" / "docker-entrypoint.sh"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_dockerfile_does_not_handle_private_keys_at_build_time():
    dockerfile = _dockerfile()

    assert "id=gpg_bot_key" not in dockerfile
    assert "id=deploy_key" not in dockerfile
    assert "/tmp/bot_secret_key.asc" not in dockerfile
    assert "/tmp/deploy_key" not in dockerfile
    assert "gpg --batch --import" not in dockerfile
    assert "cp /tmp/deploy_key" not in dockerfile


def test_compose_mounts_private_keys_only_as_runtime_secrets():
    compose = _compose()
    translator = compose["services"]["translator"]
    build = translator.get("build", {})

    assert "secrets" not in build
    assert "DEPLOY_KEY_NAME" not in "\n".join(build.get("args", []))

    service_secrets = {
        secret["source"] if isinstance(secret, dict) else secret
        for secret in translator["secrets"]
    }
    assert {"gpg_bot_key", "deploy_key"} <= service_secrets


def test_entrypoint_installs_runtime_ssh_and_gpg_secrets():
    entrypoint = _entrypoint()

    assert 'DEPLOY_KEY_PATH:-/run/secrets/deploy_key' in entrypoint
    assert 'GPG_BOT_KEY_PATH:-/run/secrets/gpg_bot_key' in entrypoint
    assert 'SKIP_GPG_IMPORT:-false' in entrypoint
    assert 'gpg --batch --import "$gpg_import_file"' in entrypoint
    assert '"$runtime_deploy_key_path" "$user_home/.ssh/deploy_key"' in entrypoint
    assert 'chmod 600 "$user_home/.ssh/deploy_key"' in entrypoint
