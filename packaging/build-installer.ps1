<#
.SYNOPSIS
    用 Inno Setup 把已经打包好的 dist\VideoDownloader 做成 Windows 安装包。

.EXAMPLE
    .\packaging\build-installer.ps1
    .\packaging\build-installer.ps1 -SkipScan
#>
[CmdletBinding()]
param(
    [switch]$SkipScan
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $PackagingDir '..')).Path
$AppDir = Join-Path $ProjectRoot 'dist\VideoDownloader'
$OutDir = Join-Path $ProjectRoot 'dist\installer'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Script = Join-Path $PackagingDir 'installer.iss'
$Scanner = Join-Path $PackagingDir 'scan_secrets.py'
# The setup name carries the version (e.g. "video DL_1.02.exe"), so it is read
# back from the compiler output instead of being duplicated here.

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==== $Text ====" -ForegroundColor Cyan
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$What = '命令'
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What 失败（退出码 $code）" }
}

Write-Host "项目根目录: $ProjectRoot" -ForegroundColor Green

Write-Step '1/6 定位 Inno Setup 编译器'
$Iscc = $null
foreach ($candidate in @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        'ISCC.exe')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $Iscc = $candidate; break }
}
if (-not $Iscc) { throw '找不到 ISCC.exe，请先安装 Inno Setup 6' }
Write-Host "  $Iscc"

Write-Step '2/6 检查待打包目录'
if (-not (Test-Path $AppDir)) { throw "缺少 $AppDir，请先运行 build.ps1" }
$files = Get-ChildItem -LiteralPath $AppDir -Recurse -File
$sizeMb = [math]::Round((($files | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
Write-Host ("  {0} 个文件, {1} MB" -f $files.Count, $sizeMb)

Write-Step '3/6 打包含前的敏感信息检查'
if ($SkipScan) {
    Write-Warning '  已跳过（-SkipScan）'
}
else {
    Invoke-Native -Exe $Python -Arguments @($Scanner, '--dist', $AppDir, '--project', $ProjectRoot) `
        -What 'dist 敏感信息扫描'
    $forbidden = Get-ChildItem -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @('.env', 'cookies.txt') -or $_.Name -eq 'secrets' -or
            $_.Extension -in @('.db', '.log', '.part', '.sqlite', '.sqlite3')
        }
    if ($forbidden) {
        $forbidden | ForEach-Object { Write-Host "  禁止项: $($_.FullName)" }
        throw '待打包目录中存在 .env / cookies.txt / secrets / 数据库 / 日志'
    }
    Write-Host '  未发现 .env / cookies.txt / secrets / 数据库 / 日志'
}

Write-Step '4/6 编译安装包'
if (Test-Path $OutDir) {
    $resolved = (Resolve-Path -LiteralPath $OutDir).Path
    if (-not $resolved.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理项目外的目录: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Invoke-Native -Exe $Iscc -Arguments @(
    "/DSourceDir=$AppDir",
    "/DInstallerOutputDir=$OutDir",
    $Script
) -What 'Inno Setup 编译'

$setupFile = Get-ChildItem -LiteralPath $OutDir -Filter *.exe -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setupFile) { throw "未生成安装包（$OutDir 下没有 .exe）" }
$setup = $setupFile.FullName
$setupMb = [math]::Round($setupFile.Length / 1MB, 1)

Write-Step '5/6 安装包敏感信息扫描（补充手段）'
if ($SkipScan) {
    Write-Warning '  已跳过（-SkipScan）'
}
else {
    Invoke-Native -Exe $Python -Arguments @($Scanner, '--file', $setup, '--project', $ProjectRoot) `
        -What '安装包扫描'
}

Write-Step '6/6 完成'
Write-Host "安装包: $setup" -ForegroundColor Green
Write-Host "大小: $setupMb MB"
Write-Host "默认安装目录: C:\Program Files\VideoDownloader"
