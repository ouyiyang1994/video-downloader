<#
.SYNOPSIS
    构建 Chrome native messaging 宿主程序 VideoDownloaderNativeHost。

.DESCRIPTION
    Chrome 用 CreateProcess 启动 native messaging 宿主，所以清单里的 path 必须是
    可执行文件。.bat 会在真正运行之前就被拒绝，表现是
    "Error when communicating with the native messaging host"，
    而且宿主自己的日志里什么都不会有（因为宿主从未启动过）。

    本脚本用 PyInstaller 生成这个宿主，默认输出到 native_host\，
    「安装登录助手」会自动使用它。

    用 --onedir 而不是 --onefile：单文件版每次启动都要把约 20 MB 解压到 %TEMP%，
    杀毒软件会对这些新文件重新扫描，实测每次启动要 100 秒左右，Chrome 会直接超时。
    目录版的启动时间在 1 秒以内。

.EXAMPLE
    .\packaging\build-native-host.ps1
    .\packaging\build-native-host.ps1 -OutputDir dist\VideoDownloader\native_host
#>
[CmdletBinding()]
param(
    [string]$OutputDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $PackagingDir '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Spec = Join-Path $PackagingDir 'native_host.spec'
$AppName = 'VideoDownloaderNativeHost'
if (-not $OutputDir) { $OutputDir = Join-Path $ProjectRoot 'native_host' }
$WorkDir = Join-Path $ProjectRoot 'build\native_host'
$AppDir = Join-Path $OutputDir $AppName

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==== $Text ====" -ForegroundColor Cyan
}

function Assert-InsideProject([string]$Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not $resolved.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理项目外的目录: $resolved"
    }
}

if (-not (Test-Path $Python)) { throw "找不到 $Python，请先创建 .venv" }

Write-Host "项目根目录: $ProjectRoot" -ForegroundColor Green

Write-Step '1/5 检查 PyInstaller'
& $Python -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) { throw '缺少 PyInstaller，请先 uv sync --dev' }

Write-Step '2/5 清理旧的构建产物'
foreach ($dir in @($WorkDir, $AppDir)) {
    if (Test-Path $dir) {
        Assert-InsideProject $dir
        Remove-Item -LiteralPath $dir -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

Write-Step '3/5 编译宿主程序'
# PyInstaller 最后会给刚写出的 exe 改 PE 时间戳与校验和；杀毒软件有时会短暂
# 占用这个新文件，导致这一步以 PermissionError 失败。实测重试即可通过。
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-Host "  第 $attempt 次尝试"
        & $Python -m PyInstaller --clean --noconfirm --distpath $OutputDir --workpath $WorkDir $Spec 2>&1 |
            ForEach-Object { Write-Host $_ }
        $code = $LASTEXITCODE
        if ($code -eq 0) { break }
        Write-Warning "  失败（退出码 $code），清理后重试"
        foreach ($dir in @($WorkDir, $AppDir)) {
            if (Test-Path $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
        }
        Start-Sleep -Seconds 3
    }
}
finally {
    $ErrorActionPreference = $previous
}
if ($code -ne 0) { throw "PyInstaller 失败（退出码 $code）" }

$HostExe = Join-Path $AppDir "$AppName.exe"
if (-not (Test-Path $HostExe)) { throw "未生成 $HostExe" }

Write-Step '4/5 自检'
$report = & $HostExe --selftest
Write-Host $report
if ($LASTEXITCODE -ne 0) { throw "宿主自检失败：$report" }

Write-Step '5/5 完成'
$files = Get-ChildItem -LiteralPath $AppDir -Recurse -File
$sizeMb = [math]::Round((($files | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
Write-Host "宿主程序: $HostExe" -ForegroundColor Green
Write-Host ("大小: {0} MB / {1} 个文件" -f $sizeMb, $files.Count)
Write-Host '下一步：在 Video Downloader 里点「Chrome 登录助手 → 安装 / 更新登录助手」。'
