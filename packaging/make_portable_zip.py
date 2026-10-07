#!/usr/bin/env python3
"""Create the Portable ZIP for a Windows release.

Why not ``Compress-Archive``?
-----------------------------
Windows PowerShell 5.1 writes ZIP entry names with **backslashes**
(``VideoDownloader\\tools\\ffmpeg\\bin\\ffmpeg.exe``).  That contradicts the ZIP
specification, which requires ``/``, and several extraction tools turn those
backslashes into literal characters in the file name.  Python's ``zipfile``
always writes conformant ``/`` separators, on every platform, so the archive
this script produces is identical no matter where it is built.

Layout
------
The archive holds exactly one top-level folder, ``VideoDownloader``, so
extracting it next to itself never scatters files into the current directory:

    VideoDownloader/
      VideoDownloader.exe
      .env.example
      README-FIRST.txt
      tools/ffmpeg/bin/{ffmpeg,ffprobe}.exe
      _internal/...

Credentials, cookies, databases and logs are rejected outright rather than
silently skipped: ``packaging/build.ps1`` has already scanned the staging
directory, so finding one here means something went wrong upstream.

Usage::

    python packaging/make_portable_zip.py \\
        --source dist/VideoDownloader \\
        --output dist/release/VideoDownloader-1.03-portable-win64.zip
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

#: Name of the single top-level folder inside the archive.
ARCHIVE_ROOT = "VideoDownloader"

#: Exact file names that must never be packaged.
FORBIDDEN_NAMES: frozenset[str] = frozenset({".env", "cookies.txt"})
FORBIDDEN_DIRNAMES: frozenset[str] = frozenset({"secrets", "downloads", "logs"})
FORBIDDEN_SUFFIXES: frozenset[str] = frozenset(
    {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".log", ".part"}
)

#: Deflate level.  6 is the usual sweet spot for a ~190 MB payload.
COMPRESS_LEVEL = 6


def is_forbidden(relative: Path) -> bool:
    if relative.name in FORBIDDEN_NAMES or relative.name.endswith("_cookies.txt"):
        return True
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    return any(part in FORBIDDEN_DIRNAMES for part in relative.parts[:-1])


def collect(source: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(files, directories)`` in a stable order."""

    files: list[Path] = []
    directories: list[Path] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if is_forbidden(relative):
            raise SystemExit(f"[失败] 待打包目录中存在禁止项: {relative}")
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files.append(relative)
    return files, directories


def build(source: Path, output: Path) -> tuple[int, int]:
    """Write the archive; return ``(entry_count, uncompressed_bytes)``."""

    files, directories = collect(source)
    if not files:
        raise SystemExit(f"[失败] {source} 中没有可打包的文件")

    output.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with zipfile.ZipFile(
        output, "w", zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
    ) as archive:
        # Directory entries first so that tools which only look at the central
        # directory still see a complete tree.
        archive.writestr(f"{ARCHIVE_ROOT}/", b"")
        for relative in directories:
            archive.writestr(f"{ARCHIVE_ROOT}/{'/'.join(relative.parts)}/", b"")
        for relative in files:
            archive.write(source / relative, f"{ARCHIVE_ROOT}/{'/'.join(relative.parts)}")
            total_bytes += (source / relative).stat().st_size

    return len(files) + len(directories) + 1, total_bytes


def verify(output: Path) -> int:
    """Re-open the archive and confirm the layout survived the round trip."""

    required = {
        f"{ARCHIVE_ROOT}/VideoDownloader.exe",
        f"{ARCHIVE_ROOT}/.env.example",
        f"{ARCHIVE_ROOT}/tools/ffmpeg/bin/ffmpeg.exe",
        f"{ARCHIVE_ROOT}/tools/ffmpeg/bin/ffprobe.exe",
    }
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        bad = archive.testzip()
        if bad is not None:
            raise SystemExit(f"[失败] 归档损坏，第一个坏条目: {bad}")
        missing = required - set(names)
        if missing:
            raise SystemExit(f"[失败] 归档缺少必需条目: {', '.join(sorted(missing))}")
        wrong_separator = [name for name in names if "\\" in name]
        if wrong_separator:
            raise SystemExit(f"[失败] 归档条目使用了反斜杠分隔符: {wrong_separator[0]}")
        tops = {name.split("/", 1)[0] for name in names}
        if tops != {ARCHIVE_ROOT}:
            raise SystemExit(f"[失败] 归档顶层目录应为 {ARCHIVE_ROOT}，实际为 {sorted(tops)}")
    return len(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Portable ZIP")
    parser.add_argument(
        "--source", type=Path, required=True, help="待打包目录，例如 dist/VideoDownloader"
    )
    parser.add_argument("--output", type=Path, required=True, help="输出的 zip 路径")
    args = parser.parse_args(argv)

    source: Path = args.source.resolve()
    if not source.is_dir():
        raise SystemExit(f"[失败] 找不到目录: {source}")
    if not (source / "VideoDownloader.exe").is_file():
        raise SystemExit(f"[失败] {source} 中缺少 VideoDownloader.exe")

    count, total_bytes = build(source, args.output)
    entries = verify(args.output)

    size_mb = args.output.stat().st_size / (1024 * 1024)
    print(f"[信息] 源目录: {source}")
    print(f"[信息] 打包 {count} 个文件（原始 {total_bytes / (1024 * 1024):.1f} MB）")
    print(f"[信息] 归档条目数: {entries}")
    print(f"[完成] {args.output} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
