"""Fail the build when a system DLL is bundled into the payload.

Why this exists
---------------
PyInstaller satisfies a DLL import by walking the process ``PATH``. On
2026-10-16 the v1.31 build ran inside a shell whose ``PATH`` also contained a
Poppler runtime
(``%USERPROFILE%\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\
native\\poppler\\Library\\bin``). PyInstaller therefore resolved ``Qt6Core.dll``'s
import of the bare name ``icuuc.dll`` to **Poppler's ICU 78** instead of the
Windows ICU 72 shim Qt 6.11 is linked against, and copied it into
``_internal\\``. There it shadowed the real one, so the frozen executable died
on ``import PySide6.QtCore`` with
``ImportError: DLL load failed while importing QtCore: 找不到指定的程序`` -
before ``setup_logging`` ran, which is why the crash left no log at all.
Along for the ride came Poppler's OpenSSL 3.6.4 (replacing Python's own
3.5.9), ``ucrtbase.dll`` and ~40 ``api-ms-win-*`` forwarder stubs.

``packaging/VideoDownloader.spec`` now drops any binary whose source is outside
the virtualenv/interpreter. This script is the independent second gate: it
inspects the *finished* ``dist/VideoDownloader/_internal`` tree, so a future
regression - a new hook, a resumed build directory, a different PyInstaller
version - still stops the release instead of shipping a broken application.

The two checks
--------------
1. **Shadowing system DLLs at the payload root** - ``_internal\\icuuc.dll``
   wins the loader's search for a bare ``icuuc.dll`` import over the copy in
   ``System32``, so a *wrong* one there is the exact v1.31 crash. The name
   check therefore only looks at the **top level of ``_internal``**, which is
   what PyInstaller puts on the search path. DLLs a wheel deliberately vendors
   inside its own package folder (``PySide6\\MSVCP140.dll``,
   ``shiboken6\\VCRUNTIME140.dll``) are that package's business and are not
   flagged - they are covered by the provenance check below instead.
2. **Foreign provenance** - every BINARY in the PyInstaller ``*.toc`` whose
   source lies outside the build's virtualenv and interpreter. This is the
   general detector, and it is what caught the whole Poppler/libheif set (ICU,
   OpenSSL, ``ucrtbase``, ~40 ``api-ms-win-*`` forwarders) rather than only the
   two files that happened to break Qt.

Usage::

    python packaging/check_bundle.py --dist dist/VideoDownloader
    python packaging/check_bundle.py --dist dist/VideoDownloader --build build/VideoDownloader
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from pathlib import Path

#: Basenames that belong to the operating system and must never shadow it at
#: the top level of ``_internal``. Compared case-insensitively; ``*`` is a
#: shell-style wildcard.
FORBIDDEN_DLL_PATTERNS: tuple[str, ...] = (
    # ICU - Windows ships icu.dll plus the icuuc/icuin shims in System32, and
    # Qt 6.11 links against *those*. Any ICU inside the bundle shadows them.
    "icuuc.dll",
    "icuin.dll",
    "icudt*.dll",
    "icu*.dll",
    # Universal C Runtime - must match the host OS exactly.
    "ucrtbase.dll",
    # API-set forwarders - resolved by the loader from System32, never shipped.
    "api-ms-win-*.dll",
)

#: Where the payload of a ``--onedir`` build lives, relative to ``--dist``.
_PAYLOAD_DIRNAME = "_internal"

#: pyinstaller TOC lines look like::
#:     ('icuuc.dll',
#:      'C:\\...\\poppler\\Library\\bin\\icuuc.dll',
#:      'BINARY'),
_TOC_ENTRY = re.compile(
    r"\(\s*'(?P<dest>[^']+)'\s*,\s*(?P<src>(?:'[^']*'|\s)+?)\s*,\s*'(?P<kind>[A-Z]+)'\s*\)",
    re.DOTALL,
)
_TOC_STRING = re.compile(r"'([^']*)'")


def _matches(name: str, patterns: tuple[str, ...]) -> str | None:
    """Return the pattern that matches ``name``, or ``None``."""

    folded = name.casefold()
    for pattern in patterns:
        regex = "^" + re.escape(pattern.casefold()).replace(r"\*", ".*") + "$"
        if re.match(regex, folded):
            return pattern
    return None


def forbidden_files(dist: Path) -> list[tuple[Path, str]]:
    """OS-owned DLLs sitting at the top level of the payload.

    Only the top level: that directory is what PyInstaller prepends to the DLL
    search path, so a file there shadows the system copy. A wheel that vendors
    the same name in its own subfolder is intentional and is left alone.
    """

    payload = dist / _PAYLOAD_DIRNAME
    root = payload if payload.is_dir() else dist
    found: list[tuple[Path, str]] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        pattern = _matches(path.name, FORBIDDEN_DLL_PATTERNS)
        if pattern:
            found.append((path.relative_to(root), pattern))
    return found


def toc_sources(build_dir: Path) -> dict[str, str]:
    """Map ``_internal`` dest name -> absolute source path, from the TOCs.

    A destination may be ``icuuc.dll`` (top level of ``_internal``) or
    ``PySide6\\Qt6Core.dll``; both are keyed by their basename so a plain
    listing of the payload can be checked against them.

    The TOC lives under the PyInstaller ``--workpath``, which is
    ``<workpath>/<spec name>/`` - not directly in the directory the build
    script passes. So the search recurses, and it skips the ``native_host``
    sub-tree, which is a separate bundle with its own (equally irrelevant)
    sources.
    """

    mapping: dict[str, str] = {}
    if not build_dir.is_dir():
        return mapping
    for toc in sorted(build_dir.rglob("*.toc")):
        if "native_host" in {part.casefold() for part in toc.parts}:
            continue
        text = toc.read_text(encoding="utf-8", errors="replace")
        for match in _TOC_ENTRY.finditer(text):
            if match.group("kind") != "BINARY":
                continue
            source = "".join(_TOC_STRING.findall(match.group("src")))
            if not source:
                continue
            dest = match.group("dest")
            mapping.setdefault(Path(dest).name.casefold(), source)
    return mapping


def foreign_sources(
    sources: dict[str, str], trusted_roots: tuple[Path, ...]
) -> list[tuple[str, str]]:
    """Sources in ``sources`` that live outside every trusted root."""

    offenders: list[tuple[str, str]] = []
    for name, source in sorted(sources.items()):
        resolved = Path(source)
        with contextlib.suppress(OSError):
            resolved = resolved.resolve()
        if not any(_is_within(resolved, root) for root in trusted_roots):
            offenders.append((name, source))
    return offenders


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def trusted_roots() -> tuple[Path, ...]:
    """The virtualenv and interpreter of the *current* process."""

    import sysconfig

    roots: list[Path] = []
    for candidate in {sys.prefix, sys.base_prefix}:
        if candidate:
            roots.append(Path(candidate).resolve())
    data_root = sysconfig.get_paths().get("data")
    if data_root:
        roots.append(Path(data_root).resolve())
    return tuple(dict.fromkeys(roots))


def build_report(dist: Path, build_dir: Path | None) -> tuple[list[str], list[str]]:
    """Return ``(failures, notes)`` for the assembled payload."""

    failures: list[str] = []
    notes: list[str] = []

    payload = dist / _PAYLOAD_DIRNAME
    if not payload.is_dir():
        return [f"找不到载荷目录: {payload}"], notes

    for relative, pattern in forbidden_files(dist):
        failures.append(f"系统 DLL 被误打包: {_PAYLOAD_DIRNAME}\\{relative}（匹配 {pattern}）")

    if build_dir is not None:
        sources = toc_sources(build_dir)
        if sources:
            offenders = foreign_sources(sources, trusted_roots())
            for name, source in offenders:
                failures.append(f"DLL 来源不受信任: {name} <- {source}")
            notes.append(f"已核对 {len(sources)} 个 BINARY 的来源")
        else:
            notes.append(f"未在 {build_dir} 找到可解析的 TOC，跳过来源核对")
    else:
        notes.append("未提供 --build，只做了文件名检查")

    if not failures:
        notes.append("未发现被误打包的系统 DLL")
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查打包产物是否混入系统 DLL")
    parser.add_argument("--dist", type=Path, required=True, help="dist/VideoDownloader 目录")
    parser.add_argument(
        "--build",
        type=Path,
        default=None,
        help="PyInstaller 的 build 目录（含 *.toc），用于核对 DLL 来源",
    )
    args = parser.parse_args(argv)

    dist: Path = args.dist.resolve()
    if not dist.is_dir():
        print(f"[错误] 找不到目录: {dist}", file=sys.stderr)
        return 2

    build_dir = args.build.resolve() if args.build else None
    failures, notes = build_report(dist, build_dir)

    for note in notes:
        print(f"[信息] {note}")
    if failures:
        print()
        for failure in failures:
            print(f"[失败] {failure}", file=sys.stderr)
        print(f"\n打包产物检查未通过：{len(failures)} 项", file=sys.stderr)
        return 1
    print("\n打包产物检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
