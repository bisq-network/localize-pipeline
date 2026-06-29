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
