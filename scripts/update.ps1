<#
.SYNOPSIS
    Video Downloader —— 更新依赖并重建镜像（Windows PowerShell）

.DESCRIPTION
    刷新基础镜像、按 pyproject.toml / uv.lock 重新安装依赖并重建镜像。

.EXAMPLE
    .\scripts\update.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Write-Info([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "  $([char]0x2713) $Text" -ForegroundColor Green }
function Fail([string]$Text) { Write-Host "  $([char]0x2717) $Text" -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail '未找到 docker' }
$null = docker info 2>&1
if ($LASTEXITCODE -ne 0) { Fail 'Docker 守护进程未运行' }

Write-Info '停止现有容器'
docker compose down --remove-orphans 2>&1 | ForEach-Object { Write-Host $_ }

Write-Info '拉取最新基础镜像并重建'
docker compose build --pull
if ($LASTEXITCODE -ne 0) { Fail '镜像构建失败' }

Write-Info '忽略缓存重建'
docker compose build --no-cache
if ($LASTEXITCODE -ne 0) { Fail '镜像重建失败' }

Write-Info '验证新镜像'
docker compose run --rm video-downloader --list-platforms
if ($LASTEXITCODE -ne 0) { Fail '验证失败' }

Write-Ok '更新完成。'
