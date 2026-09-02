"""Static checks for Docker runtime secret handling."""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile"
COMPOSE_FILE = PROJECT_ROOT / "docker" / "docker-compose.yml"
ENTRYPOINT = PROJECT_ROOT / "docker" / "docker-entrypoint.sh"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
BUILD_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "build-verify.yml"


def _dockerfile() -> str:
    """Read the production Dockerfile."""
    return DOCKERFILE.read_text(encoding="utf-8")


def _compose() -> dict:
    """Load the production Compose model."""
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _entrypoint() -> str:
    """Read the production container entrypoint."""
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_dockerfile_does_not_handle_private_keys_at_build_time():
    """Prevent private-key operations from returning to image builds."""
    dockerfile = _dockerfile()

    assert "id=gpg_bot_key" not in dockerfile
    assert "id=deploy_key" not in dockerfile
    assert "/tmp/bot_secret_key.asc" not in dockerfile
    assert "/tmp/deploy_key" not in dockerfile
    assert "gpg --batch --import" not in dockerfile
    assert "cp /tmp/deploy_key" not in dockerfile


def test_compose_mounts_private_keys_only_as_runtime_secrets():
    """Require exact read-only runtime key mounts and host sources."""
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


def test_project_profiles_stay_out_of_the_image_and_mount_read_only_at_runtime():
    """Keep project profiles out of layers and bind-mount them read-only."""
    ignore_patterns = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    compose = _compose()
    mounts = [
        mount
        for mount in compose["services"]["translator"]["volumes"]
        if isinstance(mount, dict)
    ]
    profile_mounts = {
        mount["target"]: mount
        for mount in mounts
        if str(mount.get("source", "")).startswith("../profiles/")
    }

    assert "profiles/" in ignore_patterns
    assert set(profile_mounts) == {"/app/config.yaml", "/app/glossary.json"}
    assert all(mount.get("read_only") is True for mount in profile_mounts.values())
    assert all(
        mount.get("bind", {}).get("create_host_path") is False
        for mount in profile_mounts.values()
    )


def test_ci_image_build_does_not_stage_runtime_credentials():
    """Prevent CI from staging dummy credentials into the build context."""
    workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")

    assert "Create dummy secret files" not in workflow
    assert "secrets/gpg_bot_key" not in workflow
    assert "DUMMY_KEY" not in workflow
    assert "SKIP_GPG_IMPORT=true" not in workflow
    assert "SKIP_DEPLOY_KEY=true" not in workflow
    assert "GPG_KEY_FINGERPRINT_FOR_TRUST" not in workflow


def test_entrypoint_installs_runtime_ssh_and_gpg_secrets():
    """Require entrypoint installation of runtime SSH and GPG secrets."""
    entrypoint = _entrypoint()

    assert 'DEPLOY_KEY_PATH:-/run/secrets/deploy_key' in entrypoint
    assert 'GPG_BOT_KEY_PATH:-/run/secrets/gpg_bot_key' in entrypoint
    assert 'SKIP_GPG_IMPORT:-false' in entrypoint
    assert 'gpg --batch --import "$gpg_import_file"' in entrypoint
    assert 'gpg --import-options show-only --import --with-colons "$gpg_import_file"' in entrypoint
    assert 'cp "$runtime_deploy_key_path" "$installed_deploy_key"' in entrypoint
    assert 'chmod 600 "$installed_deploy_key"' in entrypoint

    appuser_block = entrypoint.split('if [ "$(id -u)" -ne 0 ]; then', 1)[1].split("else", 1)[0]
    assert appuser_block.index("install_runtime_deploy_key") < appuser_block.index("setup_ssh")
    assert appuser_block.index("import_runtime_gpg_key") < appuser_block.index("setup_ssh")

    root_block = entrypoint.split("# --- Root Execution Block ---", 1)[1]
    assert root_block.index("install_runtime_deploy_key") < root_block.index("setup_ssh")
    assert root_block.index("import_runtime_gpg_key") < root_block.index("setup_ssh")


def test_entrypoint_reuses_runtime_secrets_after_privilege_drop():
    """Require the unprivileged re-entry path to reuse installed secrets."""
    entrypoint = _entrypoint()

    assert 'if [ -s "$user_home/.ssh/deploy_key" ]; then' in entrypoint
    assert "Runtime deploy key already installed for appuser." in entrypoint
    assert "gpg --list-secret-keys --with-colons" in entrypoint
    assert "Runtime GPG key already available for appuser." in entrypoint
