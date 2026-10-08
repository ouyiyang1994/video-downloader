"""Runs the extension's own test suite, plus static checks on its sources.

The extension is JavaScript, so its behaviour is tested with ``node --test``.
A handful of properties are also asserted statically, because they are the ones
that would be a security regression rather than a bug: a broader cookie query, a
value in a log line, or remote code.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from core.chrome_bridge import extension_dir

NODE = shutil.which("node")
LOGIC_TEST = extension_dir() / "logic.test.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 才能运行扩展的测试")


def _source(name: str) -> str:
    return (extension_dir() / name).read_text(encoding="utf-8")


def test_the_extension_test_suite_passes() -> None:
    completed = subprocess.run(
        [NODE, "--test", str(LOGIC_TEST)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(extension_dir()),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_suite_actually_ran_something() -> None:
    completed = subprocess.run(
        [NODE, "--test", str(LOGIC_TEST)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(extension_dir()),
    )
    match = re.search(r"^# pass (\d+)$", completed.stdout, re.MULTILINE)
    assert match is not None, completed.stdout
    assert int(match.group(1)) >= 20


def test_cookies_are_always_queried_per_domain() -> None:
    """``getAll({})`` would ask for the whole store; a domain filter must be used."""

    source = _source("background.js")
    assert "chrome.cookies.getAll({ domain })" in source
    assert "chrome.cookies.getAll({})" not in source
    assert "getAll(null" not in source


def test_the_extension_never_reads_a_value_from_disk() -> None:
    """It goes through the supported API only - no profile, no database."""

    for name in ("background.js", "popup.js", "logic.js"):
        source = _source(name).lower()
        for forbidden in ("filesystem", "cookies.sqlite", "indexeddb", "local state", "dpapi"):
            assert forbidden not in source, f"{name} 出现了 {forbidden}"


def test_the_extension_never_logs_or_sends_a_cookie_value() -> None:
    for name in ("background.js", "popup.js", "logic.js"):
        source = _source(name)
        for pattern in (r"console\.\w+\([^)]*\bcookie\.value", r"console\.\w+\([^)]*\bvalue\b"):
            assert not re.search(pattern, source), f"{name} 可能把 Cookie 值写进日志"


def test_the_extension_makes_no_network_request() -> None:
    """Data goes to the local process, never to a server.

    The one ``fetch`` is the loopback fallback, and it must be the only one.
    """

    for name in ("background.js", "popup.js", "logic.js"):
        source = _source(name)
        assert "XMLHttpRequest" not in source
        assert "WebSocket" not in source
        assert source.count("fetch(") == source.count("fetch(VD.localBridgeUrl()")

    # Every literal URL must be a platform login page or the loopback bridge.
    allowed = (
        "https://passport.bilibili.com/login",
        "https://www.instagram.com/accounts/login/",
        "http://127.0.0.1",
    )
    for name in ("background.js", "popup.js", "logic.js"):
        for url in re.findall(r"https?://[^\s'\"`)]+", _source(name)):
            assert url.startswith(allowed), f"{name}: {url}"


def test_the_extension_has_no_remote_or_dynamic_code() -> None:
    for name in ("background.js", "popup.js", "logic.js"):
        source = _source(name)
        assert "eval(" not in source
        assert "new Function" not in source
        assert "innerHTML" not in source


def test_the_popup_uses_text_content_not_markup() -> None:
    assert "textContent" in _source("popup.js")


def test_the_popup_does_not_touch_the_cookies_api() -> None:
    """Only the service worker talks to Chrome's cookie store."""

    assert "chrome.cookies" not in _source("popup.js")


def test_every_manifest_file_referenced_exists() -> None:
    import json

    manifest = json.loads((extension_dir() / "manifest.json").read_text(encoding="utf-8"))
    referenced = [
        manifest["background"]["service_worker"],
        manifest["action"]["default_popup"],
        *manifest["icons"].values(),
        *manifest["action"]["default_icon"].values(),
    ]
    for name in referenced:
        assert (extension_dir() / name).is_file(), name


def test_the_popup_loads_no_remote_script() -> None:
    html = (extension_dir() / "popup.html").read_text(encoding="utf-8")
    for match in re.finditer(r'<script[^>]*src="([^"]+)"', html):
        assert not match.group(1).startswith(("http:", "https:", "//")), match.group(1)
