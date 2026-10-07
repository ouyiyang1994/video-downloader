#!/usr/bin/env python3
"""Keep every declared version in the project pointing at the same number.

A Windows release touches four files that must never drift apart:

==============================  ==========================================
``pyproject.toml``              ``[project].version``
``packaging/installer.iss``     ``#define AppVersion``
``packaging/version_info.txt``  ``filevers`` / ``prodvers`` / the strings
``uv.lock``                     the root package entry (PEP 440 normalised)
==============================  ==========================================

``uv.lock`` stores the *normalised* form, so ``1.03`` is written as ``1.3``
by uv itself.  That is expected and is handled here.

Every file is read and written with ``newline=""`` so the existing line
endings survive untouched; a version bump must never show up as a
whole-file rewrite in ``git diff``.

Usage::

    python packaging/sync_version.py --show
    python packaging/sync_version.py --check 1.03
    python packaging/sync_version.py 1.03

``--check`` exits non-zero when any file disagrees.  The release workflow
gates on exactly that: a tag whose number does not match the sources must
never be allowed to produce a release.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent

PYPROJECT = PROJECT_ROOT / "pyproject.toml"
INSTALLER = PACKAGING_DIR / "installer.iss"
VERSION_INFO = PACKAGING_DIR / "version_info.txt"
UV_LOCK = PROJECT_ROOT / "uv.lock"

#: The distribution name used inside ``uv.lock``.
DIST_NAME = "video-downloader"

_VERSION_RE = re.compile(
    r"^(?P<prefix>[ \t]*version[ \t]*=[ \t]*)(?P<quote>[\"'])(?P<value>[^\"']+)(?P=quote)",
    re.MULTILINE,
)
_ISS_RE = re.compile(
    r'^(?P<prefix>[ \t]*#define[ \t]+AppVersion[ \t]+)"(?P<value>[^"]*)"', re.MULTILINE
)
_QUAD_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:filevers|prodvers)[ \t]*=[ \t]*)\((?P<value>[^)]*)\)", re.MULTILINE
)
_STRING_RE = re.compile(
    r"(?P<prefix>StringStruct\('(?:FileVersion|ProductVersion)',[ \t]*')"
    r"(?P<value>[^']*)(?P<suffix>')",
)

#: Keys reported by :func:`snapshot`, in a stable order.
QUAD_KEY = "packaging/version_info.txt (quad)"


def use_utf8_output() -> None:
    """Make the Chinese diagnostics safe on a legacy Windows code page.

    ``python packaging/sync_version.py`` is meant to be run from a plain
    PowerShell window, where stdout is often cp1252 and printing any of the
    messages below would raise UnicodeEncodeError.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


class VersionError(RuntimeError):
    """Raised when a file cannot be parsed or written."""


def parse_version(raw: str) -> tuple[int, ...]:
    """``"1.03"`` -> ``(1, 3)``.  Rejects anything that is not numeric."""

    parts = raw.strip().split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise VersionError(f"版本号必须是由点分隔的数字，收到: {raw!r}")
    numbers = tuple(int(part) for part in parts)
    if len(numbers) < 2:
        raise VersionError(f"版本号至少需要两段（例如 1.03），收到: {raw!r}")
    return numbers


def version_quad(raw: str) -> str:
    """``"1.03"`` -> ``"1, 3, 0, 0"`` (Windows file versions are numeric quads)."""

    numbers = list(parse_version(raw))[:4]
    numbers += [0] * (4 - len(numbers))
    return ", ".join(str(number) for number in numbers)


def normalised(raw: str) -> str:
    """The form uv writes into ``uv.lock`` (PEP 440 drops leading zeros)."""

    return ".".join(str(number) for number in parse_version(raw))


def _read(path: Path) -> str:
    if not path.is_file():
        raise VersionError(f"找不到文件: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# --- individual readers ------------------------------------------------------


def read_pyproject(text: str) -> str:
    match = _VERSION_RE.search(text)
    if not match:
        raise VersionError("pyproject.toml 中找不到 [project].version")
    return match.group("value")


def read_installer(text: str) -> str:
    match = _ISS_RE.search(text)
    if not match:
        raise VersionError("installer.iss 中找不到 #define AppVersion")
    return match.group("value")


def read_version_info(text: str) -> tuple[str, str]:
    """Return ``(display, quad)`` as currently declared."""

    string_match = _STRING_RE.search(text)
    quad_match = _QUAD_RE.search(text)
    if not string_match or not quad_match:
        raise VersionError("version_info.txt 中找不到版本字段")
    return string_match.group("value"), quad_match.group("value").replace(" ", "")


def read_uv_lock(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f'name = "{DIST_NAME}"':
            for candidate in lines[index + 1 : index + 6]:
                match = _VERSION_RE.match(candidate)
                if match:
                    return match.group("value")
    raise VersionError(f"uv.lock 中找不到 {DIST_NAME} 的版本")


# --- individual writers ------------------------------------------------------


def write_pyproject(text: str, version: str) -> str:
    new_text, count = _VERSION_RE.subn(
        lambda m: f"{m.group('prefix')}{m.group('quote')}{version}{m.group('quote')}", text, count=1
    )
    if count != 1:
        raise VersionError("无法写入 pyproject.toml")
    return new_text


def write_installer(text: str, version: str) -> str:
    new_text, count = _ISS_RE.subn(lambda m: f'{m.group("prefix")}"{version}"', text, count=1)
    if count != 1:
        raise VersionError("无法写入 installer.iss")
    return new_text


def write_version_info(text: str, version: str) -> str:
    quad = version_quad(version)
    text, quad_count = _QUAD_RE.subn(lambda m: f"{m.group('prefix')}({quad})", text)
    if quad_count != 2:
        raise VersionError(f"version_info.txt 中应有 2 个版本四元组，实际 {quad_count} 个")
    text, string_count = _STRING_RE.subn(
        lambda m: f"{m.group('prefix')}{version}{m.group('suffix')}", text
    )
    if string_count != 2:
        raise VersionError(f"version_info.txt 中应有 2 个版本文本，实际 {string_count} 个")
    return text


def write_uv_lock(text: str, version: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == f'name = "{DIST_NAME}"':
            for offset in range(1, 6):
                position = index + offset
                if position >= len(lines):
                    break
                match = _VERSION_RE.match(lines[position])
                if match:
                    # Everything after the matched span is the original line
                    # terminator (``\n`` or ``\r\n``); keep it verbatim.
                    terminator = lines[position][len(match.group(0)) :]
                    lines[position] = (
                        f"{match.group('prefix')}{match.group('quote')}"
                        f"{normalised(version)}{match.group('quote')}{terminator}"
                    )
                    return "".join(lines)
    raise VersionError(f"uv.lock 中找不到 {DIST_NAME} 的版本")


# --- orchestration -----------------------------------------------------------


def snapshot() -> dict[str, str]:
    """Every declared version, as it is on disk right now."""

    display, quad = read_version_info(_read(VERSION_INFO))
    return {
        "pyproject.toml": read_pyproject(_read(PYPROJECT)),
        "packaging/installer.iss": read_installer(_read(INSTALLER)),
        "packaging/version_info.txt": display,
        QUAD_KEY: quad,
        "uv.lock": read_uv_lock(_read(UV_LOCK)),
    }


def check(version: str) -> list[str]:
    """Return a list of human readable mismatches (empty when consistent)."""

    expected_quad = version_quad(version).replace(" ", "")
    expected_normalised = normalised(version)
    problems: list[str] = []
    for name, actual in snapshot().items():
        if name == QUAD_KEY:
            if actual != expected_quad:
                problems.append(f"{name}: 期望 {expected_quad}，实际 {actual}")
        elif name == "uv.lock":
            if actual != expected_normalised:
                problems.append(
                    f"{name}: 期望 {expected_normalised}（{version} 的规范形式），实际 {actual}"
                )
        elif actual != version:
            problems.append(f"{name}: 期望 {version}，实际 {actual}")
    return problems


def apply(version: str) -> list[str]:
    """Rewrite every file to ``version``.  Returns the list of touched files."""

    parse_version(version)
    touched: list[str] = []

    updates = (
        (PYPROJECT, write_pyproject),
        (INSTALLER, write_installer),
        (VERSION_INFO, write_version_info),
        (UV_LOCK, write_uv_lock),
    )
    for path, writer in updates:
        original = _read(path)
        updated = writer(original, version)
        if updated != original:
            _write(path, updated)
            touched.append(str(path.relative_to(PROJECT_ROOT)))
    return touched


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description="同步 / 校验项目各处的版本号")
    parser.add_argument("version", nargs="?", help="目标版本号，例如 1.03")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    parser.add_argument("--show", action="store_true", help="打印当前各处版本号")
    parser.add_argument(
        "--print",
        dest="print_version",
        action="store_true",
        help="只打印 pyproject.toml 中的版本号（供 CI 读取）",
    )
    args = parser.parse_args(argv)

    try:
        if args.show:
            for name, actual in snapshot().items():
                print(f"{name:38} {actual}")
            return 0

        if args.print_version:
            print(read_pyproject(_read(PYPROJECT)))
            return 0

        if not args.version:
            parser.error("需要提供版本号，或使用 --show")

        if args.check:
            problems = check(args.version)
            if problems:
                print(f"[失败] 版本号与 {args.version} 不一致：", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                print(
                    f"\n请先运行：python packaging/sync_version.py {args.version}",
                    file=sys.stderr,
                )
                return 1
            print(f"[通过] 全部版本号一致：{args.version}")
            return 0

        touched = apply(args.version)
    except VersionError as error:
        print(f"[失败] {error}", file=sys.stderr)
        return 2

    if touched:
        for name in touched:
            print(f"[已更新] {name}")
    else:
        print("[无需修改] 全部文件已是目标版本")
    print(f"[完成] 版本号统一为 {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
