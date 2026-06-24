#!/usr/bin/env bash
set -euo pipefail

# Docker-free quickstart: scaffold a minimal config.yaml by auto-detecting the
# target locales from your *.properties files.
#
# Usage:
#   ./init.sh --input-folder path/to/i18n [--target-project-root .] [--overwrite]
#   ./init.sh --input-folder path/to/i18n --api-base-url http://localhost:11434/v1   # Ollama
#
# All arguments are forwarded to `python -m src.init_config`.

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
cd "$PROJECT_ROOT"

# Prefer the project venv; fall back to system python3.
if [ -x "$PROJECT_ROOT/venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
fi

if [ -z "${PYTHON:-}" ]; then
    echo "[error] No Python interpreter found. Install Python 3.11+ or create the venv." >&2
    exit 1
fi

exec "$PYTHON" -m src.init_config "$@"
