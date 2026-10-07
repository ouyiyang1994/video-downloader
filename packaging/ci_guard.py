#!/usr/bin/env python3
"""Fail CI when anything that must stay private has been committed.

``packaging/scan_secrets.py`` guards the *build output*; this one guards the
*repository*.  It walks the tracked files (``git ls-files``) and reports:

1. **Forbidden paths** - ``.env``, ``secrets/``, ``*_cookies.txt``, keys,
   databases, logs and partial downloads.  ``.env.example`` is explicitly
   allowed because every value in it is an empty placeholder.
2. **Credential-looking content** - high-confidence patterns only (session
   cookies, private keys, cloud keys, JWTs, bearer tokens).  The patterns are
   deliberately narrow so that documentation and ``.env.example`` never trip
   them.
3. **Local absolute paths** - ``C:\\Users\\<name>\\...`` and friends.  A
   public repository must not leak the author's machine layout.  Well known
   placeholder user names used by tests are allow-listed.

Usage::

    python packaging/ci_guard.py
    python packaging/ci_guard.py --root .
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# --- rule 1: paths that must never be tracked --------------------------------

FORBIDDEN_BASENAMES: frozenset[str] = frozenset(
    {".env", "cookies.txt", "id_rsa", "id_ed25519", "credentials", ".netrc"}
)
FORBIDDEN_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".p8",
        ".jks",
        ".keystore",
        ".db",
        ".db-journal",
        ".db-wal",
        ".db-shm",
        ".sqlite",
        ".sqlite3",
        ".part",
        ".log",
    }
)
FORBIDDEN_DIRNAMES: frozenset[str] = frozenset({"secrets"})

#: Explicitly permitted even though it matches a forbidden shape.
PATH_ALLOWLIST: frozenset[str] = frozenset({".env.example"})

# --- rule 2: credential-looking content --------------------------------------

#: Substrings that mark a value as an obvious placeholder rather than a
#: credential.  Tests and documentation legitimately contain values such as
#: ``SESSDATA=super-secret-value``.
PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "secret",
    "placeholder",
    "example",
    "sample",
    "dummy",
    "fake",
    "test",
    "xxx",
    "todo",
    "changeme",
    "your",
    "redacted",
    "notreal",
    "value",
)


def _looks_like_a_real_secret(value: str) -> bool:
    """Heuristic used only by the loose patterns.

    A real session cookie is long, carries digits, and normally mixes case or
    URL-encoded punctuation.  Anything that reads like prose is ignored, which
    is what keeps ``.env.example`` and the test fixtures green.
    """

    lowered = value.lower()
    if any(token in lowered for token in PLACEHOLDER_TOKENS):
        return False
    if len(value) < 20:
        return False
    has_digit = any(character.isdigit() for character in value)
    has_mixed_shape = any(character.isupper() for character in value) or any(
        character in "%*-_" for character in value
    )
    return has_digit and has_mixed_shape


#: ``(label, pattern, validator)``.  ``validator`` is ``None`` for patterns
#: specific enough to stand on their own.
CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool] | None], ...] = (
    (
        "Bilibili SESSDATA",
        re.compile(r"SESSDATA\s*=\s*(?P<value>[A-Za-z0-9%_,*-]{20,})"),
        _looks_like_a_real_secret,
    ),
    ("私钥文件", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), None),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), None),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), None),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), None),
    ("Meta access token", re.compile(r"\bEAA[A-Za-z0-9]{60,}\b"), None),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), None),
    (
        "Bearer token",
        re.compile(r"Authorization\s*:\s*Bearer\s+(?P<value>[A-Za-z0-9._-]{20,})"),
        _looks_like_a_real_secret,
    ),
)

# --- rule 3: local absolute paths --------------------------------------------

#: A user name is restricted to characters that can actually appear in one.
#: The looser ``[^/\s"'<>]+`` also matched the regex literals in this very file
#: (``/Users/(?P<user>...`` looked like a path with the user ``(?P``).
_USER = r"(?P<user>[A-Za-z0-9._-]+)"

LOCAL_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}" + _USER),
    re.compile(r"[A-Za-z]:/Users/" + _USER),
    # The lookbehind keeps URL paths such as ``https://host/home/guide`` from
    # being mistaken for a real home directory.
    re.compile(r"(?<![\w.:/])/(?:c|C)/Users/" + _USER),
    re.compile(r"(?<![\w.:/])/(?:home|Users)/" + _USER),
)

#: Placeholder user names that legitimately appear in fixtures and docs.
USER_ALLOWLIST: frozenset[str] = frozenset(
    {"x", "user", "users", "username", "youruser", "yourname", "name", "<user>", "example", "test"}
)

#: Files larger than this are path-checked but not content-scanned.
MAX_SCAN_BYTES = 10 * 1024 * 1024
_NUL = b"\x00"


def tracked_files(root: Path) -> list[str]:
    """Repository-relative paths known to git (empty list outside a repo)."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"[失败] 无法列出被跟踪的文件（需要 git 仓库）: {error}") from error
    return [name for name in completed.stdout.decode("utf-8", "replace").split("\0") if name]


def path_problems(relative: str) -> list[str]:
    parts = Path(relative).parts
    basename = parts[-1]
    problems: list[str] = []
    if basename in PATH_ALLOWLIST:
        return problems
    if basename in FORBIDDEN_BASENAMES or basename.startswith((".env.", "id_rsa.", "id_ed25519.")):
        problems.append(f"禁止提交的文件: {relative}")
    if basename.endswith("_cookies.txt"):
        problems.append(f"禁止提交的 Cookie 文件: {relative}")
    suffix = Path(basename).suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES:
        problems.append(f"禁止提交的文件类型 ({suffix}): {relative}")
    for part in parts[:-1]:
        if part in FORBIDDEN_DIRNAMES:
            problems.append(f"禁止提交的目录: {relative}")
            break
    return problems


def content_problems(relative: str, root: Path) -> list[str]:
    path = root / relative
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if _NUL in raw[:8192]:
        return []  # binary; only the path rules apply

    text = raw.decode("utf-8", "replace")
    problems: list[str] = []
    for label, pattern, validator in CONTENT_PATTERNS:
        for match in pattern.finditer(text):
            value = match.groupdict().get("value")
            if validator is not None and value is not None and not validator(value):
                continue
            problems.append(f"疑似真实凭据（{label}）: {relative}")
            break
    for pattern in LOCAL_PATH_PATTERNS:
        for match in pattern.finditer(text):
            user = match.group("user").rstrip("\\/")
            if user.lower() in USER_ALLOWLIST:
                continue
            problems.append(f"疑似本机绝对路径（用户 {user!r}）: {relative}")
            break
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描仓库中是否混入敏感信息")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    files = tracked_files(root)

    findings: list[str] = []
    for relative in files:
        findings.extend(path_problems(relative))
        findings.extend(content_problems(relative, root))

    # De-duplicate while keeping the report order stable.
    unique: list[str] = []
    for finding in findings:
        if finding not in unique:
            unique.append(finding)

    print(f"[信息] 已检查 {len(files)} 个被跟踪的文件")
    if unique:
        print()
        for finding in unique:
            print(f"[失败] {finding}", file=sys.stderr)
        print(f"\n仓库敏感信息扫描未通过：{len(unique)} 项", file=sys.stderr)
        return 1
    print("[通过] 未发现被跟踪的敏感文件、凭据或本机绝对路径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
