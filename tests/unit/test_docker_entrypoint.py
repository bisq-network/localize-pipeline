"""Static checks for the Docker entrypoint runtime setup."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = PROJECT_ROOT / "docker" / "docker-entrypoint.sh"


def _entrypoint_script() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_entrypoint_prepares_configured_app_runtime_dirs_before_privilege_drop():
    script = _entrypoint_script()

    assert "ensure_configured_runtime_dirs()" in script
    assert 'local config_file="${TRANSLATOR_CONFIG_FILE:-/app/config.yaml}"' in script
    assert "translation_queue_folder translated_queue_folder" in script
    assert 'runtime_dir="$(resolve_app_runtime_dir "$configured_path")"' in script
    assert 'case "$runtime_dir" in' in script
    assert "/app/*)" in script
    assert 'chown "${APPUSER_UID}:${APPUSER_GID}" "$runtime_dir"' in script

    root_block_index = script.index("# --- Root Execution Block ---")
    runtime_dir_index = script.index("ensure_configured_runtime_dirs", root_block_index)
    privilege_drop_index = script.index("exec gosu appuser", root_block_index)

    assert runtime_dir_index < privilege_drop_index


def test_entrypoint_keeps_arbitrary_configured_runtime_paths_out_of_root_setup():
    script = _entrypoint_script()

    assert 'Skipping configured runtime directory outside /app: $runtime_dir' in script


def test_entrypoint_recurses_git_dir_ownership_before_privilege_drop():
    script = _entrypoint_script()

    assert 'if [ -d "/target_repo/.git" ]; then' in script
    assert 'chown -R "${APPUSER_UID}:${APPUSER_GID}" /target_repo/.git' in script


def test_entrypoint_root_fallback_is_explicit_before_exec():
    script = _entrypoint_script()

    assert "if ! exec gosu" not in script
    assert 'if [ "${ALLOW_RUN_AS_ROOT:-false}" = "true" ]; then' in script
    assert 'exec gosu appuser "$0" "$@"' in script
    assert "Root execution was requested" in script


def test_entrypoint_logs_cleanup_failures_and_removes_dead_env_chown():
    script = _entrypoint_script()

    assert "git reset --hard HEAD 2>/dev/null || true" not in script
    assert "git clean -fd 2>/dev/null || true" not in script
    assert 'log "Warning: git reset --hard failed during repository cleanup."' in script
    assert 'log "Warning: git clean failed during repository cleanup."' in script
    assert "/app/docker/.env" not in script


def test_entrypoint_safe_directory_uses_remote_url_scope():
    script = _entrypoint_script()

    assert 'git config --global --add safe.directory "$TARGET_REPO_DIR"' in script
    assert 'git config --global --add safe.directory /target_repo || true' not in script
