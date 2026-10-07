#!/usr/bin/env bash
# ============================================================================
#  Video Downloader — one-click Docker deployment (Linux / macOS / Git Bash)
# ----------------------------------------------------------------------------
#  Builds the headless CLI image, prepares a .env if needed, and runs a smoke
#  test so you know the deployment actually works.
#
#      ./scripts/deploy.sh
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

info()  { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()    { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m  ! %s\033[0m\n' "$*"; }
fail()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. prerequisites -------------------------------------------------------
info "检查 Docker"
command -v docker >/dev/null 2>&1 || fail "未找到 docker，请先安装 Docker Desktop / Docker Engine"
docker info >/dev/null 2>&1 || fail "Docker 守护进程未运行，请先启动 Docker"
ok "Docker 可用：$(docker --version)"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    fail "未找到 docker compose / docker-compose"
fi

# --- 2. configuration -------------------------------------------------------
info "准备配置"
if [ -f .env ]; then
    ok "已存在 .env，保持不动"
else
    cp .env.example .env
    ok "已从 .env.example 生成 .env（所有值均为可选，按需填写）"
fi

# --- 3. build ---------------------------------------------------------------
info "构建镜像（首次构建需下载 ffmpeg 与依赖，请耐心等待）"
"${COMPOSE[@]}" build

# --- 4. smoke test ----------------------------------------------------------
info "运行冒烟测试（列出已注册平台）"
"${COMPOSE[@]}" run --rm video-downloader --list-platforms

echo
ok "部署完成。"
echo
echo "  运行一次下载："
echo "      ./scripts/start.sh \"https://www.bilibili.com/video/BV1GJ411x7h7\""
echo "  停止并清理容器："
echo "      ./scripts/stop.sh"
echo "  更新到最新依赖："
echo "      ./scripts/update.sh"
echo
echo "  注意：本工具是命令行程序，不监听任何端口。"
