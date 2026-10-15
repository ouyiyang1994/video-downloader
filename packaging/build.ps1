<#
.SYNOPSIS
    构建 Windows GUI 版（PyInstaller --onedir）并组装可分发的目录。

.DESCRIPTION
    产物是 dist\VideoDownloader\，它同时是 Portable ZIP 的内容和 Inno Setup
    的源目录，因此这里必须把「用户拿到的所有东西」都放进去：

      VideoDownloader.exe                 主程序（PyInstaller --onedir）
      .env.example / README-FIRST.txt     模板与说明
      tools\ffmpeg\bin\                   内置 ffmpeg / ffprobe
      chrome-extension\                   Chrome 登录助手扩展（用户手动加载）
      native_host\VideoDownloaderNativeHost\
                                          native messaging 宿主（Chrome 启动它）

    最后一项由 packaging\build-native-host.ps1 单独构建：它是控制台程序、
    --onedir，和主程序互不影响，但缺少它「Chrome 登录助手」就无法工作。

.EXAMPLE
    .\packaging\build.ps1                     # 第一轮：带控制台，便于调试
    .\packaging\build.ps1 -Console false      # 验证通过后再去掉控制台
    .\packaging\build.ps1 -SkipTests          # 跳过 pytest（不推荐）
    .\packaging\build.ps1 -OutputRoot $env:TEMP\vd-build
                                              # 隔离构建：不碰项目内 dist\ 与 build\

.NOTES
    dist\ 与 build\ 里可能有上一次的产物（包括已发布的安装包）。脚本默认
    **不会**删除已有内容的目录：检测到非空目录就停止并提示。要么用
    -OutputRoot 指向临时目录做隔离构建，要么确认可以删除后显式加
    -OverwriteExisting。
#>
[CmdletBinding()]
param(
    [ValidateSet('true', 'false')][string]$Console = 'true',
    [switch]$SkipTests,
    [switch]$SkipSensitiveScan,
    #: Put dist\ and build\ under this directory instead of the project root.
    #: Point it at a temporary directory to build without touching anything
    #: already in the working tree.
    [string]$OutputRoot,
    #: Allow deleting directories that already contain files. Without it, a
    #: non-empty dist\ or build\ stops the build with an explanation.
    [switch]$OverwriteExisting
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $PackagingDir '..')).Path
$DistDir = Join-Path $ProjectRoot 'dist'
$BuildDir = Join-Path $ProjectRoot 'build'

if ($OutputRoot) {
    $resolvedRoot = if (Test-Path -LiteralPath $OutputRoot) {
        (Resolve-Path -LiteralPath $OutputRoot).Path
    }
    else {
        $full = [System.IO.Path]::GetFullPath($OutputRoot)
        New-Item -ItemType Directory -Force -Path $full | Out-Null
        $full
    }
    $DistDir = Join-Path $resolvedRoot 'dist'
    $BuildDir = Join-Path $resolvedRoot 'build'
}

$AppDir = Join-Path $DistDir 'VideoDownloader'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Spec = Join-Path $PackagingDir 'VideoDownloader.spec'
$EnvTemplate = Join-Path $ProjectRoot '.env.example'
$ReadmeFirst = Join-Path $PackagingDir 'README-FIRST.txt'
$Scanner = Join-Path $PackagingDir 'scan_secrets.py'
$NativeHostScript = Join-Path $PackagingDir 'build-native-host.ps1'
$NativeHostOut = Join-Path $AppDir 'native_host'
#: Kept under build\ on purpose: the host's PyInstaller workpath must not end
#: up inside the staging directory that gets packaged.
$NativeHostWork = Join-Path $BuildDir 'native_host'
$ExtensionSrc = Join-Path $ProjectRoot 'chrome-extension'
$ExtensionDst = Join-Path $AppDir 'chrome-extension'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==== $Text ====" -ForegroundColor Cyan
}

# 原生程序写 stderr 是正常行为（uv / PyInstaller 都会），在
# $ErrorActionPreference='Stop' 下会被当成致命错误，所以这里单独放开。
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$What = '命令'
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # 合并 stderr 并逐行转发到控制台，否则子进程输出会被 PowerShell
        # 函数的输出流吞掉，构建过程变成黑盒。
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        throw "$What 失败（退出码 $code）"
    }
}

function Assert-SafeToRemove([string]$Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path.TrimEnd('\', '/')
    $parent = Split-Path -Parent $resolved
    if (-not $parent) { throw "拒绝删除根目录: $resolved" }
    if (-not (Split-Path -Parent $parent)) { throw "拒绝删除过浅的路径: $resolved" }
    foreach ($protected in @($ProjectRoot, $env:USERPROFILE, $env:TEMP, $env:LOCALAPPDATA, $env:APPDATA)) {
        if (-not $protected) { continue }
        if ($resolved -eq $protected.TrimEnd('\', '/')) {
            throw "拒绝删除受保护目录: $resolved"
        }
    }
}

function Remove-BuildDirectory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-SafeToRemove $Path
    Remove-Item -LiteralPath (Resolve-Path -LiteralPath $Path).Path -Recurse -Force
}

Write-Host "项目根目录: $ProjectRoot" -ForegroundColor Green
Write-Host "控制台模式: $Console"

if (-not (Test-Path $Python)) {
    throw "找不到虚拟环境解释器: $Python  请先运行 uv sync"
}

Write-Step '1/8 同步依赖'
$Uv = $null
foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", 'uv')) {
    if ($candidate -and (Get-Command $candidate -ErrorAction SilentlyContinue)) {
        $Uv = $candidate
        break
    }
}
if ($Uv) {
    # ``uv sync`` 从**当前目录**往上找 pyproject.toml，而脚本其它地方都用
    # $PSScriptRoot 定位项目；从别处调用本脚本时这一步会报
    # "No pyproject.toml found in current directory or any parent directory"。
    # 固定到项目根，行为不再取决于调用者的工作目录。
    Push-Location $ProjectRoot
    try {
        Invoke-Native -Exe $Uv -Arguments @('sync', '--dev') -What 'uv sync'
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Warning '未找到 uv，跳过依赖同步（假设环境已就绪）'
}

$hasPyInstaller = $true
try {
    Invoke-Native -Exe $Python -Arguments @('-c', 'import PyInstaller') -What 'PyInstaller 检查'
}
catch {
    $hasPyInstaller = $false
}
if (-not $hasPyInstaller) {
    throw '未安装 PyInstaller，请运行 uv sync --dev'
}

if ($SkipTests) {
    Write-Step '2/8 跳过测试（-SkipTests）'
}
else {
    Write-Step '2/8 运行测试'
    # 独立的 basetemp + 关闭缓存插件：避免与沙箱进程早先创建的
    # %TEMP%\pytest-of-Maple 和 .pytest_cache 发生属主冲突。
    $testBase = Join-Path $env:TEMP ("vd-pytest-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    try {
        Invoke-Native -Exe $Python -Arguments @(
            '-m', 'pytest', '-q',
            '-p', 'no:cacheprovider',
            '--basetemp', $testBase
        ) -What 'pytest'
    }
    finally {
        if (Test-Path -LiteralPath $testBase) {
            Remove-Item -LiteralPath $testBase -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Step '3/8 清理 build\ 与 dist\'
# Never delete a directory that still holds something without an explicit
# confirmation: dist\ may hold a released installer, and a silent wipe is how a
# working release disappears. -OutputRoot avoids the question entirely.
foreach ($dir in @($BuildDir, $DistDir)) {
    if (-not (Test-Path -LiteralPath $dir)) { continue }
    $existing = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -eq 0) { continue }
    if (-not $OverwriteExisting) {
        throw ("目标目录已有内容，未获确认，拒绝删除：{0}（{1} 项）。请改用 -OutputRoot <临时目录> 做隔离构建，或确认可以删除后加 -OverwriteExisting 重跑。" -f $dir, $existing.Count)
    }
    Write-Host "  删除 $dir（已用 -OverwriteExisting 确认）"
    Remove-BuildDirectory $dir
}

Write-Step '4/8 运行 PyInstaller（--onedir）'
$env:VIDEO_DOWNLOADER_CONSOLE = $Console
# 杀软实时扫描偶尔会短暂锁住刚生成的 exe（set_exe_build_timestamp 报
# PermissionError），所以失败后清理再重试一次。
$pyiArgs = @(
    '-m', 'PyInstaller', $Spec,
    '--noconfirm', '--clean',
    '--distpath', $DistDir,
    '--workpath', $BuildDir
)
$built = $false
foreach ($attempt in 1, 2) {
    Push-Location $ProjectRoot
    try {
        Invoke-Native -Exe $Python -Arguments $pyiArgs -What "PyInstaller（第 $attempt 次）"
        $built = $true
        break
    }
    catch {
        Write-Warning "PyInstaller 第 $attempt 次失败：$($_.Exception.Message)"
        if ($attempt -eq 2) { throw }
        Start-Sleep -Seconds 5
        Remove-BuildDirectory $BuildDir
        Remove-BuildDirectory $DistDir
    }
    finally {
        Pop-Location
    }
}
if (-not $built) { throw 'PyInstaller 未产出结果' }
if (-not (Test-Path $AppDir)) { throw "未生成 $AppDir" }

Write-Step '5/8 组装外部文件（.env.example / 说明 / ffmpeg / Chrome 登录助手）'

# .env 与 secrets\ 一律不放进 dist —— 那里可能有真实凭据。
Copy-Item -LiteralPath $EnvTemplate -Destination $AppDir -Force
Copy-Item -LiteralPath $ReadmeFirst -Destination $AppDir -Force
Write-Host '  已复制 .env.example 与 README-FIRST.txt'
Write-Host '  未复制 .env / secrets\（按设计，二者只存在于用户机器上）'

$FfmpegSrc = Join-Path $ProjectRoot 'tools\ffmpeg\bin'
$FfmpegDst = Join-Path $AppDir 'tools\ffmpeg\bin'
$FfmpegTools = @('ffmpeg.exe', 'ffprobe.exe')
foreach ($tool in $FfmpegTools) {
    $source = Join-Path $FfmpegSrc $tool
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "发布必需文件缺失：$source"
    }
    try {
        $item = Get-Item -LiteralPath $source -ErrorAction Stop
        if ($item.Length -le 0) {
            throw "文件为空"
        }
    }
    catch {
        throw "发布必需文件不可用：$source（$($_.Exception.Message)）"
    }
}
New-Item -ItemType Directory -Force -Path $FfmpegDst | Out-Null
foreach ($tool in $FfmpegTools) {
    $source = Join-Path $FfmpegSrc $tool
    Copy-Item -LiteralPath $source -Destination $FfmpegDst -Force
    Write-Host "  已复制 $tool"
}
# ffplay 用不到，刻意不复制（约 163 MB）。

# --- Chrome 登录助手：扩展 + native messaging 宿主 ---------------------------
# 两件东西都必须和主程序放在一起，而且目录名不能改：
#   chrome-extension\   core/chrome_bridge.extension_dir() 按这个名字定位它，
#                       用户在 chrome://extensions 里加载的就是这个文件夹；
#   native_host\        build-native-host.ps1 的产物，install_bridge() 会把
#                       它登记给 Chrome（必要时复制到用户数据目录）。
if (-not (Test-Path -LiteralPath (Join-Path $ExtensionSrc 'manifest.json'))) {
    throw "找不到 $ExtensionSrc\manifest.json，无法打包 Chrome 登录助手扩展"
}
New-Item -ItemType Directory -Force -Path $ExtensionDst | Out-Null
$extensionCopied = 0
foreach ($file in Get-ChildItem -LiteralPath $ExtensionSrc -Recurse -File) {
    # Node 测试与图标生成脚本只在开发时用得到，不进安装包。
    if ($file.Name -in @('logic.test.js', 'make_icons.py')) { continue }
    $relative = $file.FullName.Substring($ExtensionSrc.Length).TrimStart('\', '/')
    if (($relative -split '[\\/]') -contains '__pycache__') { continue }
    $target = Join-Path $ExtensionDst $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    $extensionCopied++
}
foreach ($required in 'manifest.json', 'logic.js', 'background.js', 'popup.html', 'popup.css', 'popup.js') {
    if (-not (Test-Path -LiteralPath (Join-Path $ExtensionDst $required))) {
        throw "扩展文件不完整：缺少 $required"
    }
}
Write-Host "  已复制 Chrome 扩展（$extensionCopied 个文件）"

# 宿主由独立脚本构建：它是控制台程序（native messaging 需要可用的
# stdin/stdout），--onedir 打包，与主程序互不影响。缺少它时 Chrome 无法启动
# 登录助手，所以这里失败即整个构建失败。
Write-Host '  构建 native messaging 宿主（packaging\build-native-host.ps1）...'
& $NativeHostScript -OutputDir $NativeHostOut -WorkDir $NativeHostWork
$NativeHostExe = Join-Path $NativeHostOut 'VideoDownloaderNativeHost\VideoDownloaderNativeHost.exe'
if (-not (Test-Path -LiteralPath $NativeHostExe)) {
    throw "未生成宿主程序：$NativeHostExe"
}
Write-Host "  已生成宿主程序：$NativeHostExe"

if ($SkipSensitiveScan) {
    Write-Step '6/8 跳过敏感信息扫描（-SkipSensitiveScan）'
}
else {
    Write-Step '6/8 敏感信息扫描'
    Invoke-Native -Exe $Python -Arguments @(
        $Scanner, '--dist', $AppDir, '--project', $ProjectRoot
    ) -What '敏感信息扫描'
}

Write-Step '7/8 产物概览'
$files = Get-ChildItem -LiteralPath $AppDir -Recurse -File
$sum = ($files | Measure-Object -Property Length -Sum).Sum
$sizeMb = [math]::Round($sum / 1MB, 1)
Write-Host ("  文件数: {0}   总大小: {1} MB" -f $files.Count, $sizeMb)
foreach ($entry in Get-ChildItem -LiteralPath $AppDir) {
    if ($entry.PSIsContainer) {
        Write-Host ("  {0,-12} {1}" -f '<DIR>', $entry.Name)
    }
    else {
        $mb = [math]::Round($entry.Length / 1MB, 1)
        Write-Host ("  {0,-12} {1}" -f "$mb MB", $entry.Name)
    }
}

Write-Step '8/8 完成'
Write-Host "可分发目录: $AppDir" -ForegroundColor Green
Write-Host '下一步：把整个 VideoDownloader 文件夹拷到用户可写的目录，双击 exe 验证。'
