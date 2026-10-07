<#
.SYNOPSIS
    Video Downloader —— 停止并移除容器（Windows PowerShell）

.DESCRIPTION
    工具会自行运行结束并退出；“停止”主要用于清理残留容器（例如交互式 shell）并释放名称。

.EXAMPLE
    .\scripts\stop.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

Write-Host '==> 停止并移除容器' -ForegroundColor Cyan
docker compose down --remove-orphans 2>&1 | ForEach-Object { Write-Host $_ }

# 同时清理用 docker run 以同名启动的容器
$names = docker ps -a --format '{{.Names}}' 2>&1
if ($names -contains 'video-downloader') {
    docker rm -f video-downloader | Out-Null
    Write-Host '  已移除容器 video-downloader' -ForegroundColor Green
}

Write-Host '==> 已停止。下载的文件与日志保留在 downloads\ 与 logs\' -ForegroundColor Green
