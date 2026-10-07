<#
.SYNOPSIS
    Video Downloader —— 在 Docker 容器中运行一次下载（Windows PowerShell）

.DESCRIPTION
    本工具是命令行程序而非常驻服务，“启动”即执行一次下载任务。

.EXAMPLE
    .\scripts\start.ps1 "https://www.bilibili.com/video/BV1GJ411x7h7"
    .\scripts\start.ps1 "URL" --quality 1080p --confirm-rights
    .\scripts\start.ps1        # 不带链接时进入容器交互式 shell
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

if (-not $Args -or $Args.Count -eq 0) {
    Write-Host '未提供链接，进入容器交互式 shell（输入 exit 退出）' -ForegroundColor Cyan
    docker compose run --rm --entrypoint /bin/bash video-downloader
}
else {
    docker compose run --rm video-downloader @Args
}
exit $LASTEXITCODE
