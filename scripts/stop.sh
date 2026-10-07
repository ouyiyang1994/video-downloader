#!/usr/bin/env bash
# ============================================================================
#  Video Downloader — stop and remove the container
# ----------------------------------------------------------------------------
#      ./scripts/stop.sh
#
#  The tool runs to completion and exits on its own, so "stopping" mainly
#  means cleaning up a container that was left running (e.g. an interactive
#  shell) and freeing the name.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    COMPOSE=(docker-compose)
fi

printf '\033[36m==> 停止并移除容器\033[0m\n'
"${COMPOSE[@]}" down --remove-orphans || true

# Also catch a container started via `docker run` with the same name.
if docker ps -a --format '{{.Names}}' | grep -qx 'video-downloader'; then
    docker rm -f video-downloader >/dev/null && printf '\033[32m  ✓ 已移除容器 video-downloader\033[0m\n'
fi

printf '\033[32m==> 已停止。下载的文件与日志保留在 downloads/ 与 logs/\033[0m\n'
