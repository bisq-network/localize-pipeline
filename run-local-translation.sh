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

# Helper function to parse simple key: value pairs from the YAML config file.
yaml_get() {
    local key="$1"
    # This parser is intentionally simple. It handles unquoted, single-quoted,
    # and double-quoted values, strips inline comments, and trims whitespace.
    local value
    value=$(grep -E "^[[:space:]]*${key}:" "$CONFIG_FILE_PATH" | head -1 |
      sed -nE "s/^[[:space:]]*${key}:[[:space:]]*(\"([^\"]*)\"|'([^']*)'|([^#]*))([[:space:]]*#.*)?$/\2\3\4/p" |
      sed -E 's/^[[:space:]]*//;s/[[:space:]]*$//')
    echo "$value"
}

# --- Pre-flight Checks ---
if [ ! -f "$CONFIG_FILE_PATH" ]; then
    echo "[error] Configuration file not found: $CONFIG_FILE_PATH"
    exit 1
fi

# Read the optional glob filter from the config and export it for the Python script
TRANSLATION_FILTER_GLOB="$(yaml_get "translation_file_filter_glob" || echo "")"
if [ -n "$TRANSLATION_FILTER_GLOB" ] && [ "$TRANSLATION_FILTER_GLOB" != "null" ]; then
  export TRANSLATION_FILTER_GLOB
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

TARGET_ROOT="$(yaml_get "target_project_root" || echo "")"
INPUT_FOLDER="$(yaml_get "input_folder" || echo "")"

if [ -z "$TARGET_ROOT" ] || [ -z "$INPUT_FOLDER" ]; then
    echo "[error] 'target_project_root' or 'input_folder' not defined in $CONFIG_FILE_PATH"
    exit 1
fi

if [ ! -d "$TARGET_ROOT" ] || [ ! -d "$INPUT_FOLDER" ]; then
    echo "[error] Target project root or input folder not found. Check paths in $CONFIG_FILE_PATH"
    echo "  - target_project_root: $TARGET_ROOT"
    echo "  - input_folder: $INPUT_FOLDER"
    exit 1
fi

# --- Script Execution ---
echo "[info] Starting translation process..."
exec "$VENV_PYTHON" -m localize.cli run --config "$CONFIG_FILE_PATH"
