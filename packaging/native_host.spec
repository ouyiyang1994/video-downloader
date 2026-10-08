# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Chrome native messaging host (--onedir).

Why this exists
---------------
Chrome starts a native messaging host with ``CreateProcess``, so the manifest's
``path`` has to be a real executable. A ``.bat`` launcher is rejected before it
ever runs, and Chrome reports the failure as "Error when communicating with the
native messaging host" - with nothing at all in the host's own log, because the
host was never started. Verified on Chrome 154.

So the bridge ships two pieces:

* ``chrome-extension/`` - the extension the user loads once;
* ``VideoDownloaderNativeHost\\`` - this helper, registered by
  「安装登录助手」 under ``HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts``.

It is a small console program on purpose: native messaging needs a working
stdin/stdout, which the windowed application executable does not have.

Design notes
------------
* ``--onedir``, not ``--onefile``. A onefile build re-extracts ~20 MB into
  ``%TEMP%`` on every launch, and the antivirus rescans those fresh files each
  time - measured at ~100 s per start on Windows 11 with Defender, which would
  make the extension time out. The folder build starts in well under a second.
* Qt is excluded outright - the host never touches a widget, and PySide6 would
  multiply the size for nothing.
* ``yt_dlp.cookies`` is listed explicitly because the only use of yt-dlp here is
  the cookie jar that writes the Netscape session file; the extractors are not
  needed and are not collected.
"""

from pathlib import Path

# SPECPATH points at packaging/, so its parent is the project root.
PROJECT_ROOT = Path(SPECPATH).parent
ENTRY_POINT = PROJECT_ROOT / "core" / "native_host.py"
ICON_FILE = Path(SPECPATH) / "VideoDownloader.ico"
VERSION_FILE = Path(SPECPATH) / "version_info.txt"
APP_NAME = "VideoDownloaderNativeHost"

# The host imports these inside functions (so a development run does not pay for
# them), which makes them easy for a build to miss.
hiddenimports = [
    "config.settings",
    "core.exceptions",
    "core.models",
    "core.session_store",
    "yt_dlp.cookies",
]

EXCLUDES = [
    "PySide6",
    "PyQt5",
    "PyQt6",
    "shiboken6",
    "tkinter",
    "typer",
    "rich",
    "selectolax",
    "aiofiles",
    "matplotlib",
    "numpy",
    "pytest",
]

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=str(ICON_FILE) if ICON_FILE.is_file() else None,
    version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
