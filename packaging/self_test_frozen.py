"""Build gate: start the frozen executable and prove it can load its runtime.

``--self-test`` constructs a ``QApplication`` before it does anything else, so
exit code 2 already means "the payload started and the URL is simply
unsupported". That is a weak signal on its own: a payload missing a native
library can still exit 2 for an unrelated reason, and the dependency probe's
verdict is text rather than a code. This runner therefore asserts three things
together - exit code 2, the success marker present, the failure marker absent -
and reports the probe output so a failure says which module could not load.

A first run also writes ``.env`` next to the executable (and creates ``logs/``),
which no payload may contain: ``installer.iss`` excludes them and the packaging
scan rejects them. So the artefacts can be removed afterwards - in a ``finally``,
because a failed gate must not poison the staging folder for the next attempt.
That cleanup is **opt-in** (``--clean-first-run``): the same names are real user
data in an installed or portable folder, and a gate that silently deleted a
user's ``.env`` when pointed at the wrong directory would be worse than the
problem it solves.

Usage::

    python packaging/self_test_frozen.py --exe dist/VideoDownloader/VideoDownloader.exe \\
        --clean-first-run
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

#: The self-test's "payload is fine, this URL is unsupported" answer.
EXPECTED_EXIT_CODE = 2

#: Printed by the frozen build only after every runtime module imported.
#:
#: Deliberately ASCII: the text is captured from a child process whose stdout
#: encoding follows the host console. On a Western runner a non-ASCII marker
#: cannot be encoded at all - ``print`` raises and ``emit`` diverts the line to
#: the log - so the gate would never see it and a healthy build would fail.
SUCCESS_MARKER = "[self-test] deps ok"

#: Printed instead when at least one module could not be imported (exit 3).
FAILURE_MARKER = "依赖检查失败"

#: Never touches the network: an unsupported scheme fails platform detection
#: before any adapter is asked to fetch anything.
SELF_TEST_URL = "https://example.invalid/x"

#: Names a first run may create next to the executable. None of them belongs in
#: a payload - ``installer.iss`` excludes all four and the secret scan rejects
#: them - so they are deleted after the check.
FIRST_RUN_ARTEFACTS = (".env", "logs", "downloads", "secrets")

DEFAULT_TIMEOUT_SECONDS = 180.0


def use_utf8_output() -> None:
    """Make the Chinese diagnostics safe on a legacy Windows code page."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def decode(raw: bytes) -> str:
    """Decode a frozen build's console output.

    A windowed build writes through the console code page, which is GBK on a
    Chinese Windows and UTF-8 on a runner with ``PYTHONUTF8`` set, so both are
    tried before falling back to a lossy decode.
    """

    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_self_test(exe: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Run ``exe --self-test`` and return ``(exit code, combined output)``."""

    completed = subprocess.run(
        [str(exe), "--self-test", SELF_TEST_URL],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = decode(completed.stdout) + decode(completed.stderr)
    return completed.returncode, output.strip()


def looks_like_payload(app_dir: Path) -> bool:
    """True for a PyInstaller ``--onedir`` folder.

    The cleanup below deletes names that are ordinary user data in a source
    checkout (``.env``, ``logs/``, ``downloads/``), so it refuses to touch any
    directory that is not a packaged build.
    """

    return (app_dir / "_internal").is_dir()


def remove_first_run_artefacts(app_dir: Path) -> list[str]:
    """Delete what a first run left next to the executable.

    Only the four fixed names, only directly under ``app_dir``. Returns what was
    removed so the build log shows it rather than hiding it.
    """

    if not looks_like_payload(app_dir):
        return []
    removed: list[str] = []
    for name in FIRST_RUN_ARTEFACTS:
        target = app_dir / name
        try:
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(f"{name}/")
            elif target.exists():
                target.unlink()
                removed.append(name)
        except OSError as error:
            removed.append(f"{name}（删除失败：{type(error).__name__}）")
    return removed


def verify(
    exe: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    clean_first_run: bool = False,
) -> tuple[list[str], list[str], str]:
    """Check one frozen build.

    Returns ``(problems, removed_artefacts, output)``; an empty ``problems``
    means the payload starts and can import everything it needs.

    ``clean_first_run`` is opt-in on purpose: the names it removes are ordinary
    user data once the program is installed or unzipped for real.
    """

    exe = exe.resolve()
    if not exe.is_file():
        return [f"找不到冻结版程序：{exe}"], [], ""

    timed_out = False
    code = -1
    output = ""
    try:
        code, output = run_self_test(exe, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        removed = remove_first_run_artefacts(exe.parent) if clean_first_run else []

    if timed_out:
        return [f"自检超时（{timeout:g} 秒未退出）"], removed, output

    problems: list[str] = []
    if FAILURE_MARKER in output:
        problems.append("冻结版报告运行依赖加载失败（退出码 3）")
    if code != EXPECTED_EXIT_CODE:
        problems.append(
            f"退出码 {code}，期望 {EXPECTED_EXIT_CODE}"
            "（1 = 启动崩溃，3 = 运行依赖加载失败）"
        )
    if SUCCESS_MARKER not in output:
        problems.append(f"输出中没有「{SUCCESS_MARKER}」，不能只凭退出码判定")
    return problems, removed, output


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description="冻结版主程序自检（构建关卡）")
    parser.add_argument("--exe", type=Path, required=True, help="冻结版 VideoDownloader.exe")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"自检超时秒数（默认 {DEFAULT_TIMEOUT_SECONDS:g}）",
    )
    parser.add_argument(
        "--clean-first-run",
        action="store_true",
        help=(
            "删除首次运行在 exe 旁生成的 .env / logs / downloads / secrets。"
            "只在打包暂存目录上使用；指向已安装目录会删掉用户数据。"
        ),
    )
    args = parser.parse_args(argv)

    problems, removed, output = verify(
        args.exe, timeout=args.timeout, clean_first_run=args.clean_first_run
    )

    if output:
        print("[自检输出]")
        for line in output.splitlines():
            print(f"  {line}")
    if removed:
        print(f"[清理] 首次运行产物：{', '.join(removed)}")
    elif args.clean_first_run and not looks_like_payload(args.exe.resolve().parent):
        print("[提示] 目标不是打包目录（缺少 _internal），未做清理")

    if problems:
        for problem in problems:
            print(f"[失败] {problem}", file=sys.stderr)
        return 1
    print(f"[通过] 冻结版自检：运行依赖全部加载，退出码 {EXPECTED_EXIT_CODE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
