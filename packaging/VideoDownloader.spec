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
* **Dependency provenance guard (1.31 hot-fix).** PyInstaller resolves a DLL
  import by walking the process ``PATH``. The v1.31 build ran in a shell whose
  ``PATH`` also held a *Poppler* runtime
  (``...\\codex-runtimes\\...\\poppler\\Library\\bin``), so it satisfied
  ``Qt6Core.dll``'s ``icuuc.dll`` import with Poppler's **ICU 78** and shipped
  it in ``_internal\\`` — where it shadowed the Windows ICU 72 shim the Qt
  6.11 wheel is actually linked against. The frozen app then died on
  ``import PySide6.QtCore`` with
  ``ImportError: DLL load failed ... 找不到指定的程序`` *before* logging was
  configured, i.e. with no log file at all. The same accident swapped Python's
  own OpenSSL 3.5.9 for Poppler's 3.6.4. ``_drop_foreign_binaries`` below
  removes every binary whose *source* lies outside the virtualenv and the
  running interpreter, which is exactly the set of files that can be picked up
  only by accident. See ``packaging/check_bundle.py`` for the post-build gate
  that fails the build if such a file ever gets in again.
"""

import os
import sys
import sysconfig
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


def _trusted_binary_roots() -> tuple[Path, ...]:
    """Directories a bundled DLL is allowed to come from.

    Every file this application legitimately needs lives in one of two trees:
    the project virtualenv (PySide6, yt-dlp, …) or the Python installation
    itself (``_ssl.pyd``, its ``libssl``/``libcrypto``, the CRT the interpreter
    was built against). Anything else - a stray runtime on ``PATH``, another
    Python on the machine - can only have been picked up by accident.
    """

    roots: list[Path] = []

    # The virtualenv this build runs in (``sys.prefix``), not the interpreter's
    # base install, because PySide6/yt-dlp are installed there.
    for candidate in {sys.prefix, sys.base_prefix}:
        if candidate:
            roots.append(Path(candidate).resolve())

    # sysconfig's "data" root catches a venv whose layout differs.
    data_root = sysconfig.get_paths().get("data")
    if data_root:
        roots.append(Path(data_root).resolve())

    return tuple(dict.fromkeys(roots))


def _drop_foreign_binaries(binaries: list) -> tuple[list, list]:
    """Split ``binaries`` into (kept, dropped-abroad).

    ``binaries`` entries are ``(destination, source, type)``. An entry is kept
    when its source is unknown (empty - a synthetic entry) or lives under one
    of :func:`_trusted_binary_roots`. Everything else is a DLL some unrelated
    runtime on ``PATH`` happened to provide, and it is dropped.
    """

    trusted = _trusted_binary_roots()
    kept: list = []
    dropped: list = []
    for entry in binaries:
        source = entry[1]
        if not source:
            kept.append(entry)
            continue
        resolved = Path(source)
        try:
            resolved = resolved.resolve()
        except OSError:
            pass
        if any(_is_within(resolved, root) for root in trusted):
            kept.append(entry)
        else:
            dropped.append(entry)
    return kept, dropped


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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

# --- dependency provenance guard -------------------------------------------
# Must run *before* the EXE/COLLECT are built: dropping the entry here removes
# the file from ``_internal\`` entirely, which is what lets Qt load the Windows
# ICU shim (and Python load its own OpenSSL) instead.
a.binaries, _foreign_binaries = _drop_foreign_binaries(list(a.binaries))
if _foreign_binaries:
    print(
        "[spec] 已从产物中剔除 %d 个来源不受信任的 DLL（PATH 污染防护）:"
        % len(_foreign_binaries)
    )
    for _dest, _src, _kind in sorted(_foreign_binaries, key=lambda row: str(row[0])):
        print("        %s  <-  %s" % (_dest, _src))

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
