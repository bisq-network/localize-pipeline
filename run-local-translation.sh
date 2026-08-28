#!/usr/bin/env bash
set -euo pipefail

# This script runs the translation process locally using a virtual environment.
# It's intended for development and testing purposes.
#
# Usage:
#   ./run-local-translation.sh [path/to/config.yaml]
#
# If no config file path is provided, it defaults to 'config.yaml'.

# --- Configuration & Path Setup ---
CALLER_CWD=$(pwd)
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)

resolve_to_absolute() {
    local path="$1"
    if [[ "$path" != /* ]]; then
        path="$CALLER_CWD/$path"
    fi
    printf '%s\n' "$path"
}

# Use the first argument as the config file path, a pre-set environment value,
# or default to 'config.yaml'. Resolve relative arguments before changing dirs.
if [ -n "${1:-}" ]; then
    CONFIG_FILE_PATH=$(resolve_to_absolute "$1")
elif [ -n "${TRANSLATOR_CONFIG_FILE:-}" ]; then
    CONFIG_FILE_PATH=$(resolve_to_absolute "$TRANSLATOR_CONFIG_FILE")
else
    CONFIG_FILE_PATH="$PROJECT_ROOT/config.yaml"
fi
export TRANSLATOR_CONFIG_FILE="$CONFIG_FILE_PATH"

# The script determines the project root and changes into it.
cd "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
PIP_SYNC_BIN="$VENV_DIR/bin/pip-sync"

echo "[info] Using configuration file: $CONFIG_FILE_PATH"

# --- Pre-flight Checks ---
if [ ! -f "$CONFIG_FILE_PATH" ]; then
    echo "[error] Configuration file not found: $CONFIG_FILE_PATH"
    exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "[error] Python virtual environment not found at '$VENV_DIR'."
    echo "[info] Create it with: python3.11 -m venv venv && venv/bin/pip install -r requirements-dev.txt"
    exit 1
fi

# Ensure Python dependencies are in sync
echo "[info] Verifying Python dependencies..."
if [ ! -x "$PIP_SYNC_BIN" ]; then
    echo "[error] pip-sync not found at '$PIP_SYNC_BIN'. Install development dependencies from requirements-dev.txt."
    exit 1
fi
if ! "$PIP_SYNC_BIN" requirements-dev.txt --quiet; then
    echo "[error] Dependency sync failed. Refresh the virtual environment from requirements-dev.txt."
    exit 1
fi

# Use the same typed YAML loader and validation path as the runtime. This keeps
# quoted values, relative paths, provider settings, and future config keys in
# one implementation instead of duplicating YAML semantics in shell.
"$VENV_PYTHON" -m localize.cli check --config "$CONFIG_FILE_PATH"

# --- Script Execution ---
echo "[info] Starting translation process..."
exec "$VENV_PYTHON" -m localize.cli run --config "$CONFIG_FILE_PATH"
