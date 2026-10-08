"""Render the sign-in UI to docs/ screenshots.

Deliberately fakes the browser outlook, the bridge status **and the output
directory**, so the images carry no real account name and no local absolute
path. ``ci_guard.py`` only scans text, so a screenshot leaking a path would sail
straight through CI - keeping the sanitisation in one script is the defence.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402
from config.settings import Settings  # noqa: E402
from core import browser_cookies  # noqa: E402
from core.chrome_bridge import BridgeStatus  # noqa: E402
from core.models import Platform  # noqa: E402
from core.registry import build_registry  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs"

#: Placeholder shown in every screenshot, in place of the author's real folder.
SANITISED_OUTPUT = Path(r"C:\Users\<user>\Videos\VideoDownloader")


def fake_outlook() -> browser_cookies.AutomaticReadOutlook:
    return browser_cookies.AutomaticReadOutlook(
        default_browser="chrome",
        readable_browsers=(),
        blocked_browsers=("chrome", "edge"),
        any_installed=True,
    )


def fake_status() -> BridgeStatus:
    return BridgeStatus(
        extension_present=True,
        extension_id="deegmfcjldojkppoahflbkikhppnfepd",
        extension_version="1.0.0",
        extension_dir=Path("chrome-extension"),
        registered=True,
        registered_browsers=("chrome", "edge", "chromium", "brave"),
        expected_manifest=Path("native_host") / "com.videodownloader.cookies.json",
        session_dir=Path("secrets"),
        last_delivery=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
        host_present=True,
        launcher_kind="exe",
    )


def main() -> int:
    app = QApplication([sys.argv[0]])
    gui.automatic_read_outlook = fake_outlook
    gui.bridge_status = lambda settings: fake_status()  # noqa: ARG005 - Qt call site
    gui.webbrowser.open = lambda *a, **k: True

    settings = Settings(output_dir=SANITISED_OUTPUT)
    registry = build_registry(settings, __import__("httpx").AsyncClient())
    try:
        window = gui.MainWindow(settings, registry=registry)
        window._login_checked = True  # do not query the platforms
        window.resize(880, 940)
        window.show()
        app.processEvents()
        QTimer.singleShot(300, app.quit)
        app.exec()
        window.grab().save(str(OUT / "gui-chrome-helper.png"))
        print("wrote gui-chrome-helper.png")

        dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI), window)
        dialog.show()
        app.processEvents()
        QTimer.singleShot(300, app.quit)
        app.exec()
        dialog.grab().save(str(OUT / "gui-login-dialog.png"))
        print("wrote gui-login-dialog.png")
        dialog.bridge.stop()
        dialog.close()
    finally:
        import asyncio

        for client in registry.owned_clients:
            asyncio.run(client.aclose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
