<#
.SYNOPSIS
    用 Inno Setup 把已经打包好的 dist\VideoDownloader 做成 Windows 安装包。

.EXAMPLE
    .\packaging\build-installer.ps1
    .\packaging\build-installer.ps1 -SkipScan
    .\packaging\build-installer.ps1 -OverwriteExisting   # 确认可覆盖旧安装包

.NOTES
    dist\installer\ 里通常放着上一个已发布的安装包。脚本默认不会清空已有内容
    的输出目录：检测到非空就停止并提示，必须显式加 -OverwriteExisting 才会删除。
#>
[CmdletBinding()]
param(
    [switch]$SkipScan,
    #: Allow deleting an installer output directory that already contains
    #: files (typically the previous release's Setup.exe).
    [switch]$OverwriteExisting
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
$BundleCheck = Join-Path $PackagingDir 'check_bundle.py'
# The setup name carries the version (e.g. "video DL_1.03.exe"), so it is read
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

Write-Step '1/7 定位 Inno Setup 编译器'
$Iscc = $null
foreach ($candidate in @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        'ISCC.exe')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $Iscc = $candidate; break }
}
if (-not $Iscc) { throw '找不到 ISCC.exe，请先安装 Inno Setup 6' }
Write-Host "  $Iscc"

Write-Step '2/7 检查待打包目录'
if (-not (Test-Path $AppDir)) { throw "缺少 $AppDir，请先运行 build.ps1" }

# installer.iss 用 recursesubdirs 把整个目录搬进 {app}，所以这里只确认
# 「必须存在」的东西真的在：少了宿主程序或扩展，装出来的登录助手是坏的，
# 而那种问题要到用户机器上才会暴露。
$required = @(
    'VideoDownloader.exe',
    '.env.example',
    'tools\ffmpeg\bin\ffmpeg.exe',
    'tools\ffmpeg\bin\ffprobe.exe',
    'chrome-extension\manifest.json',
    'chrome-extension\logic.js',
    'native_host\VideoDownloaderNativeHost\VideoDownloaderNativeHost.exe'
)
foreach ($item in $required) {
    $requiredPath = Join-Path $AppDir $item
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "待打包目录缺少 $item —— 请重新运行 build.ps1（它会构建 FFmpeg 与 Chrome 登录助手）"
    }
    if ($item -like 'tools\ffmpeg\bin\*' -and (Get-Item -LiteralPath $requiredPath).Length -le 0) {
        throw "待打包目录中的 FFmpeg 文件为空：$item"
    }
}
Write-Host "  必需文件齐备（含 FFmpeg、Chrome 登录助手扩展与宿主）"

$files = Get-ChildItem -LiteralPath $AppDir -Recurse -File
$sizeMb = [math]::Round((($files | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
Write-Host ("  {0} 个文件, {1} MB" -f $files.Count, $sizeMb)

Write-Step '3/7 打包前的产物检查'
if ($SkipScan) {
    Write-Warning '  已跳过敏捷扫描（-SkipScan）'
}
else {
    Invoke-Native -Exe $Python -Arguments @($Scanner, '--dist', $AppDir, '--project', $ProjectRoot) `
        -What 'dist 敏感信息扫描'
    $forbidden = Get-ChildItem -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object {
            # Same rules as scan_secrets.py and installer.iss: a browser export is
            # <domain>_cookies.txt as often as it is cookies.txt, so the suffix is
            # checked as well. -in / -like are case-insensitive, which is exactly
            # what a Windows path comparison needs.
            $_.Name -in @('.env', 'cookies.txt') -or
            $_.Name -like '*_cookies.txt' -or
            $_.Name -eq 'secrets' -or
            $_.Extension -in @('.db', '.log', '.part', '.sqlite', '.sqlite3')
        }
    if ($forbidden) {
        $forbidden | ForEach-Object { Write-Host "  禁止项: $($_.FullName)" }
        throw '待打包目录中存在 .env / cookies.txt / secrets / 数据库 / 日志'
    }
    Write-Host '  未发现 .env / cookies.txt / secrets / 数据库 / 日志'
}

# Last gate before Inno copies the folder: a wrong ICU/OpenSSL/UCRT in
# _internal\ shadows the system copy and the installed app fails on start.
Invoke-Native -Exe $Python -Arguments @($BundleCheck, '--dist', $AppDir) `
    -What '打包产物检查（系统 DLL）'

Write-Step '4/7 编译安装包'
if (Test-Path -LiteralPath $OutDir) {
    $existingOutput = @(Get-ChildItem -LiteralPath $OutDir -Force -ErrorAction SilentlyContinue)
    if ($existingOutput.Count -gt 0 -and -not $OverwriteExisting) {
        throw ("安装包输出目录已有内容，未获确认，拒绝删除：{0}（{1} 项）。确认可以覆盖旧安装包后加 -OverwriteExisting 重跑。" -f $OutDir, $existingOutput.Count)
    }
    $resolved = (Resolve-Path -LiteralPath $OutDir).Path
    if (-not $resolved.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理项目外的目录: $resolved"
    }
    if ($existingOutput.Count -gt 0) {
        Write-Host "  删除 $OutDir（已用 -OverwriteExisting 确认）"
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

Write-Step '5/7 安装包敏感信息扫描（补充手段）'
if ($SkipScan) {
    Write-Warning '  已跳过（-SkipScan）'
}
else {
    Invoke-Native -Exe $Python -Arguments @($Scanner, '--file', $setup, '--project', $ProjectRoot) `
        -What '安装包扫描'
}

Write-Step '7/7 完成'
Write-Host "安装包: $setup" -ForegroundColor Green
Write-Host "大小: $setupMb MB"
Write-Host "默认安装目录: C:\Program Files\VideoDownloader"
