# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows GUI build (--onedir).

Design notes
------------
* The entry point is ``gui.py``; the CLI (``main.py``) is not part of this
  bundle, so typer/rich are not pulled in.
* ``.env``, ``secrets/`` and ``tools/ffmpeg`` are **never** bundled. They stay
  next to the executable and are copied by ``packaging/build.ps1``.
* Round 1 intentionally ships **no aggressive excludes**: the goal is a fully
  working build first. Trimming happens only after the functional checks pass.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH points at this directory (packaging/), so its parent is the project.
PROJECT_ROOT = Path(SPECPATH).parent
ENTRY_POINT = PROJECT_ROOT / "gui.py"
ICON_FILE = Path(SPECPATH) / "VideoDownloader.ico"
VERSION_FILE = Path(SPECPATH) / "version_info.txt"

# Without a console a startup crash would be invisible; build.ps1 flips this
# to "0" only after the first round of verification passes.
CONSOLE = os.environ.get("VIDEO_DOWNLOADER_CONSOLE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

# yt-dlp resolves its ~940 extractor modules dynamically
# (yt_dlp/extractor/__init__.py -> passthrough_module), so static analysis
# cannot see them and the build must collect the whole submodule tree.
hiddenimports = collect_submodules("yt_dlp")
hiddenimports += [
    "yt_dlp.compat",
    "yt_dlp.extractor.lazy_extractors",
]

# Round-2 candidates once the full build is verified, measured and re-tested:
#   PySide6.QtWebEngineCore / QtWebEngineWidgets / QtMultimedia* / Qt3D* /
#   QtCharts / QtQuick* / QtQml* / QtDesigner / QtSql / QtPdf* / QtTest /
#   typer / rich / selectolax / aiofiles / tenacity / tkinter / pytest
EXCLUDES: list[str] = []

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],  # never .env / secrets/ / tools/
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
    name="VideoDownloader",
    icon=str(ICON_FILE),
    version=str(VERSION_FILE),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX can corrupt Qt DLLs; keep it off.
    upx=False,
    console=CONSOLE,
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
    name="VideoDownloader",
)
