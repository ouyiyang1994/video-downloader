#!/usr/bin/env python3
"""Render the GitHub Release body for a tagged build.

The notes are generated from git history, so a release never depends on
someone remembering to write them by hand:

* commits since the previous ``v*`` tag are grouped by their conventional
  commit prefix (``feat`` -> 新功能, ``fix`` -> Bug 修复, everything else
  -> 改进);
* a 构建信息 block records exactly which commit, runner and toolchain
  produced the binaries;
* the SHA-256 block is filled from the checksum file the build produced.

Usage::

    python packaging/make_release_notes.py \\
        --version 1.03 \\
        --output dist/RELEASE_NOTES_1.03.md \\
        --checksums dist/SHA256SUMS.txt \\
        --setup-name VideoDownloader-1.03-Setup.exe \\
        --portable-name VideoDownloader-1.03-portable-win64.zip
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Conventional-commit prefixes, mapped to the section they belong to.
SECTION_BY_TYPE: dict[str, str] = {
    "feat": "新功能",
    "fix": "Bug 修复",
}
DEFAULT_SECTION = "改进"

#: Ordered section headings; empty sections are dropped from the output.
SECTION_ORDER: tuple[str, ...] = ("新功能", "Bug 修复", "改进")

_COMMIT_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<subject>.*)$"
)

COMPLIANCE_NOTE = (
    "本工具只处理**你自己拥有、已获授权，或平台明确允许下载**的公开内容。"
    "不绕过 CAPTCHA、登录限制、DRM、付费墙或任何访问控制；"
    "无法公开访问的内容会直接报错退出。请遵守当地法律与各平台服务条款。"
)


def use_utf8_output() -> None:
    """Make the Chinese diagnostics safe on a legacy Windows code page."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def git(*arguments: str) -> str:
    """Run git, returning stripped stdout (empty string when it fails)."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *arguments],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


def previous_tag() -> str:
    """The most recent ``v*`` tag before HEAD, or an empty string."""

    return git("describe", "--tags", "--abbrev=0", "--match", "v*", "HEAD^")


def commits_since(reference: str) -> list[str]:
    revision = f"{reference}..HEAD" if reference else "HEAD"
    output = git("log", "--no-merges", "--pretty=format:%s", revision)
    return [line for line in output.splitlines() if line.strip()]


def classify(subject: str) -> tuple[str, str]:
    """``("feat(gui): add X")`` -> ``("新功能", "**gui**: add X")``."""

    match = _COMMIT_RE.match(subject)
    if not match:
        return DEFAULT_SECTION, subject
    section = SECTION_BY_TYPE.get(match.group("type"), DEFAULT_SECTION)
    scope = match.group("scope")
    body = match.group("subject").strip()
    rendered = f"**{scope}**: {body}" if scope else body
    if match.group("breaking"):
        rendered = f"{rendered}（破坏性变更）"
    return section, rendered


def group_commits(commits: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {section: [] for section in SECTION_ORDER}
    for subject in commits:
        section, rendered = classify(subject)
        grouped.setdefault(section, []).append(rendered)
    return {section: entries for section, entries in grouped.items() if entries}


def read_checksums(path: Path | None) -> list[tuple[str, str]]:
    """Parse ``<sha256>  <filename>`` lines into ``(filename, hash)`` pairs."""

    if path is None or not path.is_file():
        return []
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            rows.append((parts[-1].lstrip("*"), parts[0].upper()))
    return rows


def build_info(version: str, reference: str) -> list[str]:
    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    # GitHub Actions exports this variable as "ImageOS" (mixed case) on every
    # platform; renaming it would stop it from being found on Linux runners.
    runner = os.environ.get("RUNNER_OS") or os.environ.get("ImageOS") or "Windows"  # noqa: SIM112
    arch = os.environ.get("RUNNER_ARCH") or "X64"
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    previous = reference or "（首个版本）"
    return [
        f"| 版本 | `{version}` |",
        "| --- | --- |",
        f"| Git Tag | `v{version}` |",
        f"| 提交 | `{commit}` |",
        f"| 上一个版本 | `{previous}` |",
        f"| 构建时间 | {timestamp} |",
        f"| 构建环境 | GitHub Actions · {runner} · {arch} |",
        "| 构建方式 | PyInstaller（Windows onedir）→ Inno Setup 安装包 → Portable ZIP |",
        "| 运行依赖 | 已内置 `ffmpeg` / `ffprobe`，用户无需安装 Python、uv 或任何开发环境 |",
    ]


def render(
    version: str,
    reference: str,
    sections: dict[str, list[str]],
    checksums: list[tuple[str, str]],
    setup_name: str,
    portable_name: str,
) -> str:
    lines: list[str] = [
        f"# Video Downloader v{version}",
        "",
        "多平台公开视频下载器 —— 支持 **YouTube / Instagram / 哔哩哔哩**，"
        "同时提供 **桌面 GUI** 与 **命令行 CLI**，两者共用同一套下载核心、同一份 `.env` "
        "与同一个 SQLite 历史库。",
        "",
        "## 下载",
        "",
        "| 文件 | 适用人群 | 说明 |",
        "| --- | --- | --- |",
        "| `"
        + setup_name
        + "` | 普通用户 | Windows 安装包（Inno Setup）。"
        + "双击安装，已内置 ffmpeg，无需安装 Python。 |",
        "| `"
        + portable_name
        + "` | 免安装用户 | Portable 版本。"
        + "解压到任意可写目录，双击 `VideoDownloader.exe` 即可运行。 |",
        "",
        "> 系统要求：Windows 10 / 11（x64）。两者均已内置 `ffmpeg` / `ffprobe`，无需另行安装。",
        "",
    ]

    for section in SECTION_ORDER:
        entries = sections.get(section)
        if not entries:
            continue
        lines.append(f"## {section}")
        lines.append("")
        lines.extend(f"- {entry}" for entry in entries)
        lines.append("")

    if not sections:
        lines.extend(["## 本次更新", "", "- 维护性发布，无用户可见变更。", ""])

    lines.extend(["## 构建信息", "", *build_info(version, reference), ""])

    lines.extend(["## 校验值（SHA-256）", "", "```"])
    if checksums:
        lines.extend(f"{digest}  {name}" for name, digest in checksums)
    else:
        lines.append("（未生成校验文件）")
    lines.extend(["```", ""])

    lines.extend(["## 合规声明", "", COMPLIANCE_NOTE, ""])

    lines.extend(
        [
            "## 关于自动构建",
            "",
            "本 Release 由 GitHub Actions 在推送 `v"
            + version
            + "` Tag 时自动生成：测试 → Windows 构建 → 安装包 → Portable → 发布。"
            "构建前会校验 `pyproject.toml`、`packaging/installer.iss`、"
            "`packaging/version_info.txt` 与 `uv.lock` 的版本号是否与 Tag 一致，"
            "并扫描产物中的敏感信息；任一环节失败都不会发布。",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description="生成 GitHub Release 说明")
    parser.add_argument("--version", required=True, help="版本号，例如 1.03")
    parser.add_argument("--output", type=Path, required=True, help="输出 Markdown 路径")
    parser.add_argument("--checksums", type=Path, help="SHA256SUMS 文件")
    parser.add_argument("--from-tag", default=None, help="起始 Tag（默认自动推断上一个 v* Tag）")
    parser.add_argument("--setup-name", required=True, help="安装包在 Release 中的文件名")
    parser.add_argument("--portable-name", required=True, help="Portable 包在 Release 中的文件名")
    args = parser.parse_args(argv)

    reference = args.from_tag if args.from_tag is not None else previous_tag()
    commits = commits_since(reference)
    sections = group_commits(commits)
    checksums = read_checksums(args.checksums)

    body = render(
        version=args.version,
        reference=reference,
        sections=sections,
        checksums=checksums,
        setup_name=args.setup_name,
        portable_name=args.portable_name,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8", newline="\n")

    print(f"[信息] 起始 Tag: {reference or '（无，使用全部历史）'}")
    print(f"[信息] 归类提交数: {sum(len(entries) for entries in sections.values())}")
    for section, entries in sections.items():
        print(f"  - {section}: {len(entries)} 条")
    print(f"[信息] 校验值条目: {len(checksums)}")
    print(f"[完成] 已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
