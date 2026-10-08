"""Real-Chrome end-to-end test of the extension bridge (opt-in).

Set ``VD_LIVE_EXTENSION=1`` to run it. It is opt-in because it launches a real
Chrome, writes the native messaging registry key for the current user, and needs
the extension to load - none of which belongs in an ordinary test run.

What it proves, in one pass:

1. Chrome loads the bundled extension and gives it the pinned ID, so the
   ``allowed_origins`` entry the application wrote is the right one;
2. ``chrome.cookies.set`` / ``chrome.cookies.getAll`` work from the extension,
   which is the whole point - Chrome decrypts its own store;
3. the extension cannot see a domain it was not granted;
4. ``chrome.runtime.sendNativeMessage`` reaches the generated launcher, starts
   ``core.native_host``, and the cookie lands in the managed session file.

Everything runs against a throw-away Chrome profile and a throw-away session
directory; the registry key is removed again in ``finally``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from config.settings import Settings
from core import chrome_bridge, chrome_cdp
from core.chrome_cdp import WebSocket, _command
from core.local_bridge import LocalBridge

pytestmark = pytest.mark.skipif(
    os.environ.get("VD_LIVE_EXTENSION") != "1",
    reason="设置 VD_LIVE_EXTENSION=1 才会真的加载 Chrome 扩展",
)

EXTENSION_READY_TIMEOUT = 40.0


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path / "downloads",
        database_path=tmp_path / "downloads" / "test.db",
        log_dir=tmp_path / "logs",
        session_dir=tmp_path / "secrets",
        native_host_dir=tmp_path / "native_host",
        chrome_profile_dir=tmp_path / "chrome-profile",
    )


def _provision_host(settings: Settings) -> None:
    """Put the compiled host where the bridge will look for it.

    Chrome can only start an executable, so the bridge under test has to be the
    real thing - and it has to be in place *before* ``install_bridge`` writes the
    manifest, exactly as it is in the application. This calls the production
    helper rather than re-implementing the copy, so the test exercises the same
    code path an installed application does.
    """

    if chrome_bridge.provision_host(settings) is None:
        pytest.skip("尚未构建宿主程序，请先运行 packaging/build-native-host.ps1")


def _targets(instance: chrome_cdp.DebugChrome) -> list[dict[str, Any]]:
    listing = chrome_cdp._http_json(instance, "/json/list")  # noqa: SLF001 - test helper
    return listing if isinstance(listing, list) else []


def _worker_url(instance: chrome_cdp.DebugChrome, extension_id: str) -> str | None:
    for target in _targets(instance):
        url = str(target.get("url") or "")
        if target.get("type") == "service_worker" and url.startswith(
            f"chrome-extension://{extension_id}/"
        ):
            return str(target.get("webSocketDebuggerUrl") or "") or None
    return None


def _extension_page_url(instance: chrome_cdp.DebugChrome, extension_id: str) -> str | None:
    """The WebSocket URL of the extension's *popup page*.

    Deliberately not "any extension target": the service worker shares the
    ``chrome-extension://<id>/`` prefix but runs in a worker global, where the
    ``chrome.*`` bindings the popup uses are not the same object.
    """

    prefix = f"chrome-extension://{extension_id}/popup.html"
    for target in _targets(instance):
        if target.get("type") == "page" and str(target.get("url") or "").startswith(prefix):
            return str(target.get("webSocketDebuggerUrl") or "") or None
    return None


def _open_popup(instance: chrome_cdp.DebugChrome, extension_id: str) -> None:
    """Open the popup as a tab so its JavaScript can be driven directly."""

    with WebSocket(_browser_socket_url(instance), timeout=30.0) as browser:
        _command(
            browser,
            1,
            "Target.createTarget",
            {"url": f"chrome-extension://{extension_id}/popup.html"},
        )


def _wait_for_popup(instance: chrome_cdp.DebugChrome, extension_id: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = _extension_page_url(instance, extension_id)
        if url:
            return url
        time.sleep(0.5)
    raise AssertionError("扩展页面没有打开")


def _wait_for_worker(instance: chrome_cdp.DebugChrome, extension_id: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = _worker_url(instance, extension_id)
        if url:
            return url
        time.sleep(0.5)
    raise AssertionError("Chrome 没有启动扩展的 service worker")


def _browser_socket_url(instance: chrome_cdp.DebugChrome) -> str:
    """The browser-level debugger socket, where extension management lives."""

    version = chrome_cdp._http_json(instance, "/json/version")  # noqa: SLF001
    return str(version["webSocketDebuggerUrl"])


def _evaluate(socket_: WebSocket, identifier: int, expression: str) -> Any:
    result = _command(
        socket_,
        identifier,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
    )
    if "exceptionDetails" in result:
        raise AssertionError(f"扩展内执行失败：{json.dumps(result['exceptionDetails'])[:400]}")
    return result.get("result", {}).get("value")


def test_extension_to_native_host_end_to_end(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    extension_id = chrome_bridge.extension_id()

    _provision_host(settings)
    install = chrome_bridge.install_bridge(settings)
    ok, detail = chrome_bridge.verify_host(settings)
    assert ok, detail
    assert install.registered_browsers, "至少要为 Chrome 写入注册表项"

    executable = chrome_cdp.chrome_executable()
    assert executable is not None, "本机没有 Chrome"

    directory = chrome_cdp.profile_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    port = chrome_cdp._free_port()  # noqa: SLF001
    extension_path = chrome_bridge.extension_dir()

    # ``--load-extension`` was removed from Chrome in 137, so the extension is
    # loaded over CDP instead. That is a test-harness detail only: a real user
    # loads it from chrome://extensions, which is unaffected.
    command = [
        str(executable),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={directory}",
        "--enable-unsafe-extension-debugging",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "about:blank",
    ]
    import subprocess

    process = subprocess.Popen(  # noqa: S603 - fixed path, fixed flags
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True
    )
    instance = chrome_cdp.DebugChrome(
        process=process, port=port, profile_dir=directory, executable=executable
    )

    # The loopback fallback, exactly as the sign-in window would run it. The
    # extension tries native messaging first; where the OS refuses to give a
    # native host its pipes, this is the route that still works.
    bridge = LocalBridge(settings)
    bridge.start()

    try:
        assert chrome_cdp._wait_for_endpoint(instance, timeout=40.0), (  # noqa: SLF001
            "Chrome 没有开启调试端口"
        )

        # Chrome itself must agree with the ID we derived from the pinned key -
        # that ID is exactly what the native messaging manifest allows.
        with WebSocket(_browser_socket_url(instance), timeout=30.0) as browser:
            loaded = _command(browser, 1, "Extensions.loadUnpacked", {"path": str(extension_path)})
        assert loaded.get("id") == extension_id, loaded

        # The service worker must exist, i.e. Chrome accepted the background
        # script. Its execution context is a worker global, so the interactive
        # part of the test runs in the popup page instead - which is also the
        # surface a user actually clicks.
        assert _wait_for_worker(instance, extension_id, timeout=EXTENSION_READY_TIMEOUT)

        _open_popup(instance, extension_id)
        popup = _wait_for_popup(instance, extension_id, timeout=EXTENSION_READY_TIMEOUT)

        with WebSocket(popup, timeout=30.0) as page:
            # 1. the page is really served by our extension
            assert _evaluate(page, 1, "chrome.runtime.id") == extension_id

            # 2. Chrome's own cookie API works, including for a freshly set value
            set_result = _evaluate(
                page,
                2,
                """chrome.cookies.set({
                    url: 'https://www.bilibili.com/',
                    name: 'SESSDATA',
                    value: 'E2E-FAKE-VALUE',
                    path: '/',
                    secure: true,
                }).then(c => c ? c.name : 'FAILED')""",
            )
            assert set_result == "SESSDATA", set_result

            visible = _evaluate(
                page,
                3,
                """chrome.cookies.getAll({domain: 'bilibili.com'})
                    .then(list => list.map(c => c.name).sort())""",
            )
            assert "SESSDATA" in visible, visible

            # 3. a domain the extension was not granted is invisible to it
            foreign = _evaluate(
                page,
                4,
                """chrome.cookies.getAll({domain: 'example.com'})
                    .then(list => list.length)""",
            )
            assert foreign == 0, foreign

            # 4. the whole pipeline: extension -> service worker -> native host
            response = _evaluate(
                page,
                5,
                """chrome.runtime.sendMessage({type: 'send', platform: 'bilibili'})""",
            )
            assert isinstance(response, dict), response
            assert response.get("ok") is True, response
            assert response.get("cookieNames") == ["SESSDATA"], response
            assert "E2E-FAKE-VALUE" not in json.dumps(response), "响应里不能出现 Cookie 值"

            # 5. the popup's own rendering must not have leaked a value either
            rendered = _evaluate(page, 6, "document.getElementById('status').textContent")
            assert isinstance(rendered, str) and rendered
            assert "E2E-FAKE-VALUE" not in rendered
    finally:
        instance.stop()
        bridge.stop()
        chrome_bridge.uninstall_bridge(settings)

    session = settings.session_dir / "bilibili_cookies.txt"
    assert session.is_file(), "扩展没有把会话写到 secrets/"
    body = session.read_text(encoding="utf-8")
    assert "SESSDATA" in body and "E2E-FAKE-VALUE" in body
    assert "example.com" not in body, "只应写入平台自己的域名"
    assert not chrome_bridge.is_registered(settings), "测试必须清理注册表"
