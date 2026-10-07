#!/usr/bin/env bash
# ============================================================================
#  Video Downloader — launch the desktop GUI (Linux / macOS / Git Bash)
# ----------------------------------------------------------------------------
#      ./scripts/run-gui.sh
#
#  Prefers uv (which prepares the Python 3.12 environment automatically) and
#  falls back to an existing .venv.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
    exec uv run python gui.py
fi

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python gui.py
fi

printf '\033[33m未找到 uv，也没有可用的 .venv。请先执行： uv sync --dev\033[0m\n' >&2
exit 1
