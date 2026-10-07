<#
.SYNOPSIS
    Video Downloader —— 启动桌面 GUI（Windows PowerShell）

.DESCRIPTION
    优先使用 uv（自动准备 3.12 环境）；否则回退到已存在的 .venv。

.EXAMPLE
    .\scripts\run-gui.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

$Uv = $null
foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", 'uv')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $Uv = $candidate; break }
}

if ($Uv) {
    & $Uv run python gui.py
    exit $LASTEXITCODE
}

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $VenvPython) {
    & $VenvPython gui.py
    exit $LASTEXITCODE
}

Write-Host '未找到 uv，也没有可用的 .venv。请先执行：' -ForegroundColor Yellow
Write-Host '    uv sync --dev' -ForegroundColor Yellow
Write-Host '或参考 README 的「本地运行」章节。' -ForegroundColor Yellow
exit 1
