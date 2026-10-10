"""Fail the build when credentials end up inside the distribution.

Three independent checks run against ``dist/VideoDownloader``:

1. **Structural** - no ``.env``, no ``secrets/``, no cookie/database/log files.
2. **Value based** - the real values from the developer's ``.env`` and
   ``secrets/cookies.txt`` must not appear in any file, at any offset.
3. **Keyword** - cookie *names* such as ``sessionid``/``SESSDATA``/``csrftoken``.
   These are only a hard failure **outside** the bundled payloads
   (``_internal/`` holds yt-dlp and Qt; ``tools/`` holds ffmpeg, whose HTTP
   stack references those names too; ``native_host/`` is the compiled native
   messaging host, another PyInstaller bundle; ``chrome-extension/`` is the
   extension, which has to name the cookies it is allowed to read) and outside
   ``.env.example``, whose sensitive-looking lines are empty placeholders.
   The *value* check below applies everywhere, including those folders.

Credential *values* are only taken from keys whose name marks them as secrets
(``*_KEY``/``*_TOKEN``/``*_COOKIE``/``*SESSDATA``/``*SECRET``/``*PASSWORD*``),
plus every cookie value in ``secrets/cookies.txt``. Configuration values such as
``OUTPUT_DIR`` or ``YTDLP_COOKIEFILE`` are not credentials.

``8090`` is deliberately **not** treated as a credential: it is the local proxy
port of the user's existing configuration, and flagging it would break a
supported setup.

Usage::

    python packaging/scan_secrets.py --dist dist/VideoDownloader --project .
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Cookie/token *names* that should never appear outside third-party code.
SENSITIVE_KEYWORDS: tuple[bytes, ...] = (
    b"sessionid",
    b"sessionid_ss",
    b"SESSDATA",
    b"csrftoken",
    b"ds_user_id",
    b"bili_jct",
    b"access_token",
    b"Authorization: Bearer",
)

#: Marker that proves a cookies.txt was copied into the bundle.
COOKIE_FILE_MARKER: bytes = b"Netscape HTTP Cookie File"

#: Third-party payload. Keyword hits here are informational only.
#: ``tools/`` is listed because the bundled ffmpeg/ffprobe binaries contain
#: cookie-related strings in their own HTTP implementation. ``native_host/`` is
#: the compiled native messaging host - a PyInstaller ``--onedir`` bundle of its
#: own, with the same ``_internal/`` payload (yt-dlp's cookie jar included) as
#: the main build, so it is third-party code by the same argument.
INFORMATIONAL_PREFIXES: tuple[str, ...] = ("_internal", "tools", "native_host")

#: First-level folders whose files are allowed to *name* the cookies they handle.
#: The bundled Chrome extension has to declare ``SESSDATA`` / ``sessionid`` /
#: ``csrftoken`` to do its job at all, so a keyword hit there is expected. This
#: only relaxes the *keyword* rule: the value-based check still applies, which
#: is the one that actually guards credentials.
KEYWORD_EXEMPT_PREFIXES: tuple[str, ...] = ("chrome-extension",)

#: Shipped template: every sensitive-looking line is an empty placeholder.
#: A real value here would still be caught by the value check.
KEYWORD_EXEMPT_FILENAMES: frozenset[str] = frozenset({".env.example"})

#: Files that must never ship (``.env.example`` is explicitly allowed).
#: Compared case-insensitively - see :func:`structural_violations`.
FORBIDDEN_FILENAMES: frozenset[str] = frozenset({".env", "cookies.txt"})

#: Filename *endings* that must never ship. A browser export is
#: ``<domain>_cookies.txt`` at least as often as it is ``cookies.txt``, and every
#: managed session under ``secrets/`` uses exactly that shape - so a session file
#: that somehow escaped its folder is still caught by name, not only by the
#: value check that depends on the developer's own credentials being readable.
FORBIDDEN_FILENAME_SUFFIXES: tuple[str, ...] = ("_cookies.txt",)

FORBIDDEN_DIRNAMES: frozenset[str] = frozenset({"secrets"})
FORBIDDEN_SUFFIXES: frozenset[str] = frozenset({".db", ".part", ".log", ".sqlite", ".sqlite3"})

#: Configuration keys whose values are settings, not credentials. In particular
#: the local proxy port 8090 is a supported configuration value, never a secret.
NON_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "PROXY_BYPASS_PLATFORMS",
        "YTDLP_COOKIEFILE",
        "BILIBILI_COOKIEFILE",
        "YTDLP_COOKIES_FROM_BROWSER",
        "OUTPUT_DIR",
        "DATABASE_PATH",
        "LOG_DIR",
        "LOG_LEVEL",
        "DIR_TEMPLATE",
        "FFMPEG_PATH",
        "REQUEST_TIMEOUT",
        "MAX_RETRIES",
        "CONCURRENT_FRAGMENTS",
        "CONFIRM_RIGHTS",
        "SKIP_EXISTING",
        "USER_AGENT",
        "CHUNK_SIZE",
    }
)

#: A key whose name contains one of these marks its value as a credential.
SECRET_KEY_HINTS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "COOKIE",
    "SESSDATA",
    "PASSWORD",
    "PASSWD",
)

#: Values shorter than this are treated as placeholders, not credentials.
MIN_SECRET_LENGTH = 12
_CHUNK = 4 * 1024 * 1024


def collect_secret_values(project_root: Path) -> list[bytes]:
    """Real credential *values* from ``.env`` and ``secrets/cookies.txt``."""

    values: list[bytes] = []

    env_file = project_root / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            key = key.strip().upper()
            if key in NON_SECRET_KEYS:
                continue
            if not any(hint in key for hint in SECRET_KEY_HINTS):
                continue
            raw_value = raw.strip()
            if len(raw_value) >= MIN_SECRET_LENGTH:
                values.append(raw_value.encode("utf-8"))

    cookies = project_root / "secrets" / "cookies.txt"
    if cookies.is_file():
        for line in cookies.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and len(parts[6]) >= MIN_SECRET_LENGTH:
                values.append(parts[6].encode("utf-8"))

    unique: list[bytes] = []
    for secret in values:
        if secret not in unique:
            unique.append(secret)
    return unique


def structural_violations(dist: Path) -> list[str]:
    """Files and directories that must never be part of the distribution.

    Every name is folded to lower case before it is compared. Windows treats
    ``.ENV``, ``Cookies.TXT`` and ``SECRETS\\`` as the very same files as their
    lower-case spellings, so a case-sensitive check would report a clean tree
    while the installer happily packed a real credential file.
    """

    problems: list[str] = []
    for path in sorted(dist.rglob("*")):
        relative = path.relative_to(dist)
        name = path.name.casefold()
        if path.is_dir():
            if name in FORBIDDEN_DIRNAMES:
                problems.append(f"禁止的目录: {relative}")
            continue
        if not path.is_file():
            continue
        if name in FORBIDDEN_FILENAMES or name.endswith(FORBIDDEN_FILENAME_SUFFIXES):
            problems.append(f"禁止的文件: {relative}")
        elif path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            problems.append(f"禁止的文件类型: {relative}")
    return problems


def find_needles(path: Path, needles: list[bytes]) -> list[bytes]:
    """Return the needles present in ``path`` (chunked, binary safe)."""

    if not needles:
        return []
    hits: list[bytes] = []
    overlap = max(len(needle) for needle in needles) - 1
    tail = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                for needle in needles:
                    if needle not in hits and needle in window:
                        hits.append(needle)
                if len(hits) == len(needles):
                    break
                tail = window[-overlap:] if overlap > 0 else b""
    except OSError:
        return hits
    return hits


def scan_for_values(dist: Path, needles: list[bytes]) -> list[tuple[Path, list[bytes]]]:
    """Every file containing one of ``needles`` - always a hard failure."""

    found: list[tuple[Path, list[bytes]]] = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        hits = find_needles(path, needles)
        if hits:
            found.append((path.relative_to(dist), hits))
    return found


def scan_for_keywords(
    dist: Path, keywords: tuple[bytes, ...] = SENSITIVE_KEYWORDS
) -> tuple[list[tuple[Path, list[bytes]]], list[tuple[Path, list[bytes]]]]:
    """Split keyword hits into (hard failures, informational hits).

    A hit inside the bundled third-party payload is expected - yt-dlp source
    refers to cookie names, and so does the shipped extension - so it is
    reported but does not fail the build.
    """

    hard: list[tuple[Path, list[bytes]]] = []
    soft: list[tuple[Path, list[bytes]]] = []
    exempt = INFORMATIONAL_PREFIXES + KEYWORD_EXEMPT_PREFIXES
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        hits = find_needles(path, list(keywords))
        if not hits:
            continue
        relative = path.relative_to(dist)
        if path.name in KEYWORD_EXEMPT_FILENAMES:
            continue
        if relative.parts and relative.parts[0] in exempt:
            soft.append((relative, hits))
        else:
            hard.append((relative, hits))
    return hard, soft


def build_report(dist: Path, project_root: Path) -> tuple[list[str], list[str]]:
    """Return ``(failures, notes)`` for the whole distribution."""

    failures: list[str] = []
    notes: list[str] = []

    failures.extend(structural_violations(dist))

    secrets = collect_secret_values(project_root)
    notes.append(f"从 .env 与 cookies.txt 提取到 {len(secrets)} 个待检查值")
    for relative, hits in scan_for_values(dist, secrets):
        failures.append(f"发现真实凭据值: {relative} ({len(hits)} 处)")

    # The cookie-file marker follows the same rules: a cookies.txt is fatal
    # anywhere, but ffmpeg/ffprobe reference the format in their own binaries.
    marker_hard, marker_soft = scan_for_keywords(dist, (COOKIE_FILE_MARKER,))
    for relative, _hits in marker_hard:
        failures.append(f"发现 Cookie 文件内容: {relative}")
    if marker_soft:
        notes.append(f"{len(marker_soft)} 个第三方二进制包含 Cookie 文件格式字符串（非凭据）")

    hard, soft = scan_for_keywords(dist)
    for relative, hits in hard:
        names = ", ".join(hit.decode("utf-8", "replace") for hit in hits)
        failures.append(f"敏感关键字出现在非第三方文件: {relative} ({names})")
    if soft:
        notes.append(f"{len(soft)} 个随包文件包含 Cookie 名称字符串（扩展 / yt-dlp 引用，非凭据）")
    if not soft:
        notes.append("未在第三方载荷中发现 Cookie 名称字符串")

    notes.append("已按配置忽略本地代理端口 8090（非凭据）")
    return failures, notes


def scan_single_file(path: Path, project_root: Path) -> tuple[list[str], list[str]]:
    """Best-effort scan of a build artefact such as the installer.

    A compressed installer cannot be searched exhaustively, so this only looks
    at the raw bytes. The authoritative guarantees are the staging-directory
    scan plus the explicit ``Excludes`` list in ``installer.iss``.
    """

    notes = [
        "安装包是压缩归档，字节级扫描只是补充手段；权威检查是 dist 扫描 + installer.iss 的 Excludes"
    ]
    if not path.is_file():
        return [f"找不到文件: {path}"], notes

    needles = collect_secret_values(project_root)
    hits = find_needles(path, [*needles, COOKIE_FILE_MARKER])
    failures: list[str] = []
    for hit in hits:
        if hit == COOKIE_FILE_MARKER:
            failures.append(f"安装包中出现 Cookie 文件标记: {path.name}")
        else:
            failures.append(f"安装包中出现真实凭据值: {path.name}")
    if not hits:
        notes.append(f"未在 {path.name} 的明文字节中发现凭据（检查了 {len(needles)} 个值）")
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描打包产物中的敏感信息")
    parser.add_argument("--dist", type=Path, help="dist/VideoDownloader 目录")
    parser.add_argument("--file", type=Path, help="单个产物文件（例如安装包）")
    parser.add_argument("--project", default=Path("."), type=Path, help="项目根目录")
    args = parser.parse_args(argv)

    project: Path = args.project.resolve()
    if not args.dist and not args.file:
        print("[错误] 需要 --dist 或 --file", file=sys.stderr)
        return 2

    if args.file:
        failures, notes = scan_single_file(args.file.resolve(), project)
    else:
        dist: Path = args.dist.resolve()
        if not dist.is_dir():
            print(f"[错误] 找不到目录: {dist}", file=sys.stderr)
            return 2
        failures, notes = build_report(dist, project)

    for note in notes:
        print(f"[信息] {note}")
    if failures:
        print()
        for failure in failures:
            print(f"[失败] {failure}", file=sys.stderr)
        print(f"\n敏感信息扫描未通过：{len(failures)} 项", file=sys.stderr)
        return 1
    print("\n敏感信息扫描通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
