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
    build_args = build.get("args", [])
    if isinstance(build_args, dict):
        build_args_text = "\n".join(f"{key}={value}" for key, value in build_args.items())
    else:
        build_args_text = "\n".join(build_args)
    assert "DEPLOY_KEY_NAME" not in build_args_text

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
    assert (
        compose["secrets"]["gpg_bot_key"]["file"]
        == "${GPG_BOT_KEY_FILE:-../secrets/gpg_bot_key/bot_secret_key.asc}"
    )
    assert (
        compose["secrets"]["deploy_key"]["file"]
        == "${DEPLOY_KEY_FILE:-../secrets/deploy_key/id_ed25519}"
    )


def test_entrypoint_installs_runtime_ssh_and_gpg_secrets():
    entrypoint = _entrypoint()

    assert 'DEPLOY_KEY_PATH:-/run/secrets/deploy_key' in entrypoint
    assert 'GPG_BOT_KEY_PATH:-/run/secrets/gpg_bot_key' in entrypoint
    assert 'SKIP_GPG_IMPORT:-false' in entrypoint
    assert 'gpg --batch --import "$gpg_import_file"' in entrypoint
    assert 'gpg --import-options show-only --with-colons "$gpg_import_file"' in entrypoint
    assert '"$runtime_deploy_key_path" "$user_home/.ssh/deploy_key"' in entrypoint
    assert 'chmod 600 "$user_home/.ssh/deploy_key"' in entrypoint

    appuser_block = entrypoint.split('if [ "$(id -u)" -ne 0 ]; then', 1)[1].split("else", 1)[0]
    assert appuser_block.index("install_runtime_deploy_key") < appuser_block.index("setup_ssh")
    assert appuser_block.index("import_runtime_gpg_key") < appuser_block.index("setup_ssh")

    root_block = entrypoint.split("# --- Root Execution Block ---", 1)[1]
    assert root_block.index("install_runtime_deploy_key") < root_block.index("setup_ssh")
    assert root_block.index("import_runtime_gpg_key") < root_block.index("setup_ssh")
