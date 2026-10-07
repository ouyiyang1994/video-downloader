<#
.SYNOPSIS
    Video Downloader —— 一键 Docker 部署（Windows PowerShell）

.DESCRIPTION
    构建无界面 CLI 镜像、按需生成 .env，并执行冒烟测试确认部署可用。

.EXAMPLE
    .\scripts\deploy.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Write-Info([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "  $([char]0x2713) $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "  ! $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host "  $([char]0x2717) $Text" -ForegroundColor Red; exit 1 }

# --- 1. 前置检查 ------------------------------------------------------------
Write-Info '检查 Docker'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail '未找到 docker，请先安装 Docker Desktop'
}
$null = docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail 'Docker 守护进程未运行，请先启动 Docker Desktop'
}
Write-Ok "Docker 可用：$((docker --version) -join '')"

$null = docker compose version 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail '未找到 docker compose 插件（请升级到带 Compose V2 的 Docker Desktop）'
}

# --- 2. 配置 ----------------------------------------------------------------
Write-Info '准备配置'
if (Test-Path (Join-Path $Root '.env')) {
    Write-Ok '已存在 .env，保持不动'
}
else {
    Copy-Item -LiteralPath (Join-Path $Root '.env.example') -Destination (Join-Path $Root '.env')
    Write-Ok '已从 .env.example 生成 .env（所有值均为可选，按需填写）'
}

# --- 3. 构建 ----------------------------------------------------------------
Write-Info '构建镜像（首次构建需下载 ffmpeg 与依赖，请耐心等待）'
docker compose build
if ($LASTEXITCODE -ne 0) { Fail '镜像构建失败' }

# --- 4. 冒烟测试 ------------------------------------------------------------
Write-Info '运行冒烟测试（列出已注册平台）'
docker compose run --rm video-downloader --list-platforms
if ($LASTEXITCODE -ne 0) { Fail '冒烟测试失败' }

Write-Host ''
Write-Ok '部署完成。'
Write-Host ''
Write-Host '  运行一次下载：'
Write-Host '      .\scripts\start.ps1 "https://www.bilibili.com/video/BV1GJ411x7h7"'
Write-Host '  停止并清理容器：'
Write-Host '      .\scripts\stop.ps1'
Write-Host '  更新到最新依赖：'
Write-Host '      .\scripts\update.ps1'
Write-Host ''
Write-Host '  注意：本工具是命令行程序，不监听任何端口。'
