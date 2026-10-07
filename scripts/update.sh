#!/usr/bin/env bash
# ============================================================================
#  Video Downloader — update to the latest dependencies and rebuild
# ----------------------------------------------------------------------------
#      ./scripts/update.sh
#
#  Refreshes the base image, reinstalls the locked dependencies from
#  pyproject.toml / uv.lock and rebuilds the image.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "未找到 docker"
docker info >/dev/null 2>&1 || fail "Docker 守护进程未运行"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    COMPOSE=(docker-compose)
fi

info "停止现有容器"
"${COMPOSE[@]}" down --remove-orphans || true

info "拉取最新基础镜像"
"${COMPOSE[@]}" build --pull

info "重建镜像（忽略缓存）"
"${COMPOSE[@]}" build --no-cache

info "验证新镜像"
"${COMPOSE[@]}" run --rm video-downloader --list-platforms

ok "更新完成。"
