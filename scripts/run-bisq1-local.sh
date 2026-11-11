#!/usr/bin/env bash
set -euo pipefail
PIPELINE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOCALIZE_PYTHON="${LOCALIZE_PYTHON:-$PIPELINE_ROOT/venv/bin/python}"
if [ ! -x "$LOCALIZE_PYTHON" ]; then
    echo "Set LOCALIZE_PYTHON to a pipeline virtual environment's Python." >&2
    exit 1
fi
exec "$LOCALIZE_PYTHON" "$PIPELINE_ROOT/scripts/run-bisq1-local.py" "$@"
