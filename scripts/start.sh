#!/usr/bin/env bash
# ============================================================================
#  Video Downloader — run a download in the Docker container
# ----------------------------------------------------------------------------
#      ./scripts/start.sh "https://www.bilibili.com/video/BV1GJ411x7h7"
#      ./scripts/start.sh "URL" --quality 1080p --confirm-rights
#      ./scripts/start.sh                       # no URL -> interactive shell
#
#  This is a CLI, not a service: "starting" means running one download.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    COMPOSE=(docker-compose)
fi

if [ "$#" -eq 0 ]; then
    printf '\033[36m未提供链接，进入容器交互式 shell（输入 exit 退出）\033[0m\n'
    exec "${COMPOSE[@]}" run --rm --entrypoint /bin/bash video-downloader
fi

exec "${COMPOSE[@]}" run --rm video-downloader "$@"
