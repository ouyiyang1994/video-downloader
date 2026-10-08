"""Application side of the Chrome extension bridge.

The extension can only reach this application through Chrome's *native
messaging* channel, and Chrome only opens that channel when three things line
up:

1. a manifest file exists on disk describing the host program;
2. a registry key under ``HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts``
   points at that manifest;
3. the manifest's ``allowed_origins`` names the extension's ID.

This module owns all three, plus the deterministic extension ID. Everything it
writes lives under the application's data root (``native_host/``) and in
``HKCU`` - the per-user hive, so no administrator rights are needed and nothing
outside this user's account is touched.

Layout
------
The build ships two folders next to the executable, and both are handled here::

    VideoDownloader/                 <- APP_ROOT
      VideoDownloader.exe
      chrome-extension/              <- the extension the user loads once
      native_host/VideoDownloaderNativeHost/   <- the compiled host

``native_host/`` is the *staging* location. Where the application folder is not
writable - an install under ``C:\\Program Files`` - the host is copied into the
data root by :func:`provision_host` before the manifest is written, because the
manifest has to name a path the user can execute and the host needs a writable
folder for its own ``host-config.json``. In a portable layout the two folders
are the same and nothing is copied.

Why an extension at all: Chromium 127+ encrypts its cookie database with
App-Bound Encryption and yt-dlp cannot decrypt it. ``chrome.cookies`` is the
supported way to read a cookie, and Chrome does the decryption itself. This
project does not extract keys, inject into a browser, or patch anything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import APP_ROOT, Settings
from core.exceptions import ChromeBridgeError

logger = logging.getLogger(__name__)

try:  # pragma: no cover - only absent off Windows
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

#: Native messaging host name. Must equal ``core.native_host.HOST_NAME`` and the
#: ``HOST_NAME`` constant in ``chrome-extension/logic.js``.
HOST_NAME = "com.videodownloader.cookies"

#: Folder in the repository (and next to a packaged build) holding the extension.
EXTENSION_DIRNAME = "chrome-extension"

#: Folder under the data root that holds the generated launcher and manifest.
HOST_DIRNAME = "native_host"

HOST_MANIFEST_NAME = f"{HOST_NAME}.json"

#: Read by the compiled host to learn where the sessions live. Must match
#: ``core.native_host.HOST_CONFIG_NAME``.
HOST_CONFIG_NAME = "host-config.json"

#: A console-mode helper built from ``core.native_host`` by
#: ``packaging/native-host.spec``. Native messaging needs a real executable with
#: a working stdin/stdout: Chrome starts the host with ``CreateProcess``, so a
#: ``.bat`` is rejected before it runs, and the windowed application executable
#: has no stdio to speak the protocol on.
PACKAGED_HOST_NAME = "VideoDownloaderNativeHost.exe"

#: The ``--onedir`` folder the helper ships in. A onefile build re-extracts into
#: ``%TEMP%`` on every launch and the antivirus rescans it, which measured ~100 s
#: per start - long enough that Chrome would give up on the host.
HOST_FOLDER_NAME = "VideoDownloaderNativeHost"

#: Development fallback. It runs the module with the project's interpreter and
#: is fine for ``--selftest``, but Chrome cannot start it - see the note above.
LAUNCHER_SCRIPT_NAME = "host.bat"

#: Chromium-family browsers, and the registry root each one reads for native
#: messaging hosts. All are under HKEY_CURRENT_USER.
REGISTRY_ROOTS: tuple[tuple[str, str], ...] = (
    ("chrome", r"Software\Google\Chrome\NativeMessagingHosts"),
    ("edge", r"Software\Microsoft\Edge\NativeMessagingHosts"),
    ("chromium", r"Software\Chromium\NativeMessagingHosts"),
    ("brave", r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts"),
)

#: Browser key -> executable locations, relative to an environment variable.
BROWSER_EXECUTABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "chrome": (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ),
    "edge": (
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
    ),
    "brave": (
        ("PROGRAMFILES", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("PROGRAMFILES(X86)", "BraveSoftware/Brave-Browser/Application/brave.exe"),
    ),
    "chromium": (
        ("LOCALAPPDATA", "Chromium/Application/chrome.exe"),
        ("PROGRAMFILES", "Chromium/Application/chrome.exe"),
    ),
}

#: Chromium renders its own settings pages, so they must be opened by the
#: browser itself rather than through the shell.
EXTENSIONS_PAGE = "chrome://extensions/"

#: Chrome's rule for turning a public key into an extension ID: the first 16
#: bytes of SHA-256 over the DER key, hex-encoded, with each digit shifted into
#: the a-p range.
_ID_TRANSLATION = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


def extension_dir() -> Path:
    """The bundled extension source, next to the application."""

    return APP_ROOT / EXTENSION_DIRNAME


def read_extension_manifest(directory: Path | None = None) -> dict[str, Any]:
    """Parse the extension's ``manifest.json``.

    Raises :class:`ChromeBridgeError` when it is missing or unreadable - that is
    a packaging problem, not something the user can fix by retrying.
    """

    path = (directory or extension_dir()) / "manifest.json"
    if not path.is_file():
        raise ChromeBridgeError(
            "找不到 Chrome 扩展文件",
            detail=f"缺少 {path}。请确认安装包完整，或从项目目录运行。",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChromeBridgeError(
            "Chrome 扩展清单无法解析",
            detail=f"{path.name}：{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict):
        raise ChromeBridgeError("Chrome 扩展清单格式不正确", detail=path.name)
    return data


def extension_id_from_key(key: str) -> str:
    """Derive the extension ID from the manifest's pinned public key."""

    try:
        der = base64.b64decode(key, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ChromeBridgeError(
            "扩展公钥无法解析",
            detail=f"manifest.json 的 key 字段不是合法的 base64：{type(exc).__name__}",
        ) from exc
    if not der:
        raise ChromeBridgeError("扩展公钥为空", detail="manifest.json 的 key 字段为空")
    return hashlib.sha256(der).hexdigest()[:32].translate(_ID_TRANSLATION)


def extension_id(directory: Path | None = None) -> str:
    """The extension's stable ID, or raise when the key is missing."""

    manifest = read_extension_manifest(directory)
    key = manifest.get("key")
    if not isinstance(key, str) or not key:
        raise ChromeBridgeError(
            "扩展清单缺少 key 字段",
            detail="没有固定的公钥，Chrome 会按目录生成扩展 ID，本程序无法预先登记。",
        )
    return extension_id_from_key(key)


def extension_version(directory: Path | None = None) -> str:
    version = read_extension_manifest(directory).get("version")
    return version if isinstance(version, str) else ""


def host_dir(settings: Settings) -> Path:
    """Where the generated launcher and host manifest live.

    Configurable so a test never writes into the developer's real checkout.
    """

    return settings.resolve_path(settings.native_host_dir)


def native_host_exe(settings: Settings) -> Path:
    """The compiled host Chrome will actually start."""

    return host_dir(settings) / HOST_FOLDER_NAME / PACKAGED_HOST_NAME


def bundled_host_dir() -> Path:
    """The host folder as the build produced it, next to the application.

    ``packaging/build.ps1`` writes it there (``build-native-host.ps1`` builds it
    directly in a checkout), and it travels into ``dist/VideoDownloader`` and
    then into the installer unchanged.
    """

    return APP_ROOT / HOST_DIRNAME / HOST_FOLDER_NAME


def bundled_host_executable() -> Path:
    """The host that ships with (or was built for) this installation.

    Kept separate from :func:`native_host_exe` because the two differ in a
    development checkout: the build writes next to the project, while the
    installed bridge looks under the data root. :func:`provision_host` is what
    bridges the two.
    """

    return bundled_host_dir() / PACKAGED_HOST_NAME


def launcher_path(settings: Settings) -> Path:
    """The program the native messaging manifest points at.

    Prefers the compiled host; falls back to the development ``.bat`` so that
    ``--selftest`` still works in a checkout. Chrome itself needs the ``.exe``,
    which is why :attr:`BridgeStatus.host_present` tracks only that one.
    """

    executable = native_host_exe(settings)
    if executable.is_file():
        return executable
    return host_dir(settings) / LAUNCHER_SCRIPT_NAME


#: Suffix of the half-written copy ``provision_host`` builds before swapping it
#: in. Only ever seen after an interrupted install.
_STAGING_SUFFIX = ".staging"


def _staging_dir(directory: Path) -> Path:
    return directory.with_name(directory.name + _STAGING_SUFFIX)


def _host_is_current(source: Path, target: Path) -> bool:
    """True when the installed copy already matches the build output.

    ``shutil.copytree`` preserves timestamps, so a copy this module made is the
    same size *and* the same age. That lets a repeated 「安装 / 更新登录助手」
    do nothing instead of rewriting ~40 MB - and, just as importantly, instead of
    failing because Chrome happens to be running the host at that moment.
    """

    try:
        source_stat = source.stat()
        target_stat = target.stat()
    except OSError:
        return False
    return (source_stat.st_size, int(source_stat.st_mtime)) == (
        target_stat.st_size,
        int(target_stat.st_mtime),
    )


def provision_host(settings: Settings) -> Path | None:
    """Make the compiled host available where the manifest is about to point.

    The build ships the helper inside the application folder, and in a portable
    layout - where the application folder *is* the data root - that is already
    the right place, so nothing is copied.

    An installation under ``C:\\Program Files`` is different. The manifest has
    to name a path the current user can execute, and the host reads its
    ``host-config.json`` from its own folder - which has to be writable. Neither
    holds there, so the whole ``--onedir`` folder is copied into the data root,
    next to the manifest and the development launcher.

    Returns the executable, or ``None`` when the build produced no host (a
    checkout that has not run ``packaging/build-native-host.ps1``), in which case
    the development ``.bat`` launcher stays in charge.
    """

    source = bundled_host_executable()
    if not source.is_file():
        return None

    target = native_host_exe(settings)
    if target == source:
        # Portable layout: the build output *is* the installed host.
        return target
    if _host_is_current(source, target):
        logger.info("登录助手宿主已是最新，跳过复制：%s", target)
        return target

    directory = target.parent
    staging = _staging_dir(directory)
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        # Copy first, swap afterwards. A failure while copying then leaves the
        # previous host exactly where it was - and Chrome may well be running
        # it at this very moment, because the user just clicked a button in the
        # application it is talking to.
        shutil.copytree(source.parent, staging)
        if directory.exists():
            shutil.rmtree(directory)
        staging.rename(directory)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if target.is_file():
            logger.warning("更新登录助手宿主失败，继续使用现有副本：%s", type(exc).__name__)
            return target
        raise ChromeBridgeError(
            "无法安装登录助手宿主程序",
            detail=f"复制 {source.parent} 到 {directory} 失败：{type(exc).__name__}",
        ) from exc

    logger.info("已安装登录助手宿主：%s", target)
    return target


def _remove_tree(path: Path, *, boundary: Path) -> bool:
    """Delete ``path``, but only when it really lives inside ``boundary``.

    Uninstalling the helper removes the host copy this module made; it must
    never be able to touch anything else on the machine, whatever the settings
    happen to say.
    """

    try:
        resolved = path.resolve()
        root = boundary.resolve()
    except OSError:
        return False
    if resolved == root or not resolved.is_relative_to(root):
        logger.warning("拒绝删除预期之外的目录：%s", resolved)
        return False
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        logger.warning("删除 %s 失败：%s", resolved.name, type(exc).__name__)
        return False
    return True


def host_manifest_path(settings: Settings) -> Path:
    return host_dir(settings) / HOST_MANIFEST_NAME


def session_dir(settings: Settings) -> Path:
    from core.session_store import session_dir as _session_dir

    return _session_dir(settings)


def browser_executable(browser: str) -> Path | None:
    """Locate a Chromium-family browser's executable, if it is installed."""

    for variable, relative in BROWSER_EXECUTABLES.get(browser, ()):
        base = os.environ.get(variable)
        if not base:
            continue
        candidate = Path(base) / relative
        if candidate.is_file():
            return candidate
    return None


# --- launcher and manifest ---------------------------------------------------


#: Exported by the launcher so the host never has to guess where the sessions
#: live. Must match ``core.native_host.SESSION_DIR_ENV``.
_SESSION_DIR_ENV = "VIDEO_DOWNLOADER_SESSION_DIR"


def _python_for_launcher() -> str:
    """The interpreter the launcher should use in a development checkout."""

    executable = Path(sys.executable)
    if executable.is_file() and "python" in executable.name.lower():
        return str(executable)
    from shutil import which

    return which("python") or "python"


def launcher_source(settings: Settings) -> str:
    """The ``.bat`` that starts the host.

    ``chcp 65001`` switches the console to UTF-8 before any path is read, so a
    project living under a non-ASCII directory still resolves; ``>nul`` keeps
    the code-page banner off stdout, which is the native messaging channel.
    """

    return (
        "@echo off\r\n"
        "rem Generated by Video Downloader. Safe to delete; it will be recreated.\r\n"
        "chcp 65001 >nul\r\n"
        f'set "{_SESSION_DIR_ENV}={session_dir(settings)}"\r\n'
        f'cd /d "{APP_ROOT}"\r\n'
        f'"{_python_for_launcher()}" -m core.native_host %*\r\n'
    )


def write_launcher(settings: Settings) -> Path:
    """Write the launcher and return its path."""

    directory = host_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    path = launcher_path(settings)
    if path.suffix.lower() == ".exe":
        return path
    path.write_text(launcher_source(settings), encoding="utf-8", newline="")
    logger.info("已生成登录助手宿主启动器：%s", path.name)
    return path


def host_config_path(settings: Settings) -> Path:
    """Where the host's own config file goes: beside whichever launcher is used.

    Chrome gives a native messaging host no arguments and no custom environment,
    so this file is how the compiled host learns where the sessions live.
    """

    return launcher_path(settings).parent / HOST_CONFIG_NAME


def write_host_config(settings: Settings) -> Path:
    """Tell the host which directories belong to this installation."""

    launcher = launcher_path(settings)
    payload = {
        "sessionDir": str(session_dir(settings)),
        "logDir": str(settings.resolve_path(settings.log_dir)),
    }
    path = launcher.parent / HOST_CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("已写入宿主配置：%s", path.name)
    return path


def write_host_manifest(settings: Settings, *, launcher: Path | None = None) -> Path:
    """Write the native messaging manifest Chrome reads."""

    target = launcher or launcher_path(settings)
    if not target.is_file():
        raise ChromeBridgeError(
            "登录助手宿主程序不存在",
            detail=f"缺少 {target}。请重新执行「安装登录助手」。",
        )
    payload = {
        "name": HOST_NAME,
        "description": "Video Downloader 登录助手：把浏览器会话交给本机程序",
        "path": str(target),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id()}/"],
    }
    path = host_manifest_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("已写入 native messaging 清单：%s", path.name)
    return path


# --- registry ----------------------------------------------------------------


def registry_entries(settings: Settings) -> dict[str, str]:
    """Every ``HKCU`` native messaging key and the path it should hold."""

    expected = str(host_manifest_path(settings))
    return {root: expected for _browser, root in REGISTRY_ROOTS}


def registered_path(browser_root: str) -> str | None:
    """What ``browser_root`` currently points at, or ``None``."""

    if winreg is None:  # pragma: no cover - non-Windows
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, browser_root + "\\" + HOST_NAME) as handle:
            value = winreg.QueryValueEx(handle, "")[0]
    except OSError:
        return None
    return str(value)


def is_registered(settings: Settings) -> bool:
    """True when at least one browser already points at our manifest."""

    expected = str(host_manifest_path(settings))
    return any(registered_path(root) == expected for _browser, root in REGISTRY_ROOTS)


def _write_registry_value(browser_root: str, value: str) -> bool:
    if winreg is None:  # pragma: no cover - non-Windows
        return False
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, browser_root + "\\" + HOST_NAME, 0, winreg.KEY_WRITE
        )
        with key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, value)
    except OSError as exc:
        logger.warning("写入注册表失败（%s）：%s", browser_root, type(exc).__name__)
        return False
    return True


def _delete_registry_key(browser_root: str) -> bool:
    if winreg is None:  # pragma: no cover - non-Windows
        return False
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, browser_root + "\\" + HOST_NAME)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("删除注册表项失败（%s）：%s", browser_root, type(exc).__name__)
        return False
    return True


# --- public operations -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BridgeStatus:
    """Everything the GUI needs to describe the bridge to the user."""

    extension_present: bool
    extension_id: str | None
    extension_version: str | None
    extension_dir: Path | None
    registered: bool
    registered_browsers: tuple[str, ...]
    expected_manifest: Path
    session_dir: Path
    last_delivery: datetime | None
    #: True only when the compiled host exists - the one Chrome can start.
    host_present: bool
    #: ``"exe"``, ``"script"`` (development fallback) or ``"missing"``.
    launcher_kind: str = "missing"
    #: True when the build shipped a host that just has not been provisioned
    #: yet. It changes the advice: "install the helper", not "reinstall".
    host_bundled: bool = False
    #: True when the provisioned copy is older than the one this build ships -
    #: i.e. the application was upgraded and the helper has not been refreshed.
    host_stale: bool = False

    @property
    def ready(self) -> bool:
        """True when nothing is missing on this machine."""

        return self.extension_present and self.registered and self.host_present

    @property
    def delivered_ever(self) -> bool:
        return self.last_delivery is not None

    def summary(self) -> str:
        if not self.extension_present:
            return "扩展文件缺失，请重新安装 Video Downloader。"
        if not self.host_present:
            if self.launcher_kind == "script":
                return (
                    "登录助手尚不可用：本机只有开发用的脚本启动器，"
                    "Chrome 只能启动可执行文件，需要先构建宿主程序。"
                )
            if self.host_bundled:
                return "登录助手尚未安装：宿主程序已随本程序就位，只差登记这一步。"
            return "登录助手尚未安装（缺少宿主程序）。"
        if not self.registered:
            return "登录助手尚未安装：扩展已就绪，等待注册本机宿主程序。"
        if self.host_stale:
            return "登录助手已就绪，但宿主程序还是升级前的版本。"
        if self.delivered_ever:
            return "登录助手已就绪，并且已经收到过浏览器会话。"
        return "登录助手已就绪，等待你在扩展里发送会话。"

    def advice(self) -> str:
        """What the user should do next, in one sentence."""

        if not self.host_present:
            if self.launcher_kind == "script":
                return (
                    "开发环境请先运行 packaging/build-native-host.ps1 生成宿主程序，"
                    "然后再点「安装 / 更新登录助手」。"
                )
            if self.host_bundled:
                return "点「安装 / 更新登录助手」，本程序会把随包提供的宿主程序登记到当前用户。"
            return "请重新安装 Video Downloader，安装包会带上宿主程序。"
        if self.host_stale:
            # The copy still speaks the protocol, so the bridge works - but it is
            # the previous release's build, and only a click refreshes it.
            return "点「安装 / 更新登录助手」，把宿主程序刷新到当前版本（旧副本仍然可用）。"
        if self.ready:
            return (
                "在 Chrome 里打开扩展「Video Downloader 登录助手」，"
                "选择平台后点「发送到 Video Downloader」。"
            )
        if not self.extension_present:
            return "重新安装 Video Downloader 即可恢复扩展文件。"
        return "点「安装登录助手」，然后在打开的 chrome://extensions 页面加载扩展。"


def _last_delivery(settings: Settings) -> datetime | None:
    """When the extension last handed a session over, if ever."""

    from core.models import Platform
    from core.session_store import session_file

    newest: float | None = None
    for platform in Platform:
        path = session_file(settings, platform)
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=UTC)


def bridge_status(settings: Settings) -> BridgeStatus:
    """Inspect the bridge without changing anything."""

    try:
        read_extension_manifest()
        present = True
        identifier: str | None = extension_id()
        version = extension_version() or None
        directory: Path | None = extension_dir()
    except ChromeBridgeError:
        present = False
        identifier = None
        version = None
        directory = None

    expected = host_manifest_path(settings)
    expected_value = str(expected)
    holders: list[str] = []
    for browser, root in REGISTRY_ROOTS:
        if registered_path(root) == expected_value:
            holders.append(browser)

    executable = native_host_exe(settings)
    if executable.is_file():
        launcher_kind = "exe"
    elif (host_dir(settings) / LAUNCHER_SCRIPT_NAME).is_file():
        launcher_kind = "script"
    else:
        launcher_kind = "missing"

    bundled = bundled_host_executable()
    host_stale = (
        launcher_kind == "exe"
        and bundled.is_file()
        # A portable layout has one and the same folder, so it is never stale.
        and executable != bundled
        and not _host_is_current(bundled, executable)
    )

    return BridgeStatus(
        extension_present=present,
        extension_id=identifier,
        extension_version=version,
        extension_dir=directory,
        registered=bool(holders),
        registered_browsers=tuple(holders),
        expected_manifest=expected,
        session_dir=session_dir(settings),
        last_delivery=_last_delivery(settings),
        host_present=launcher_kind == "exe",
        launcher_kind=launcher_kind,
        host_bundled=bundled.is_file(),
        host_stale=host_stale,
    )


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of :func:`install_bridge`."""

    status: BridgeStatus
    manifest_path: Path
    launcher: Path
    registered_browsers: tuple[str, ...]
    extension_dir: Path


def install_bridge(settings: Settings) -> InstallResult:
    """Write the launcher and manifest, then point every Chromium browser at it.

    Idempotent: running it again refreshes both files, which is also how an
    upgraded application updates the launcher. The compiled host is provisioned
    first, so the manifest can never name a path where no host exists.
    """

    identifier = extension_id()  # raises early when the extension is missing
    directory = host_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)

    provision_host(settings)
    launcher = write_launcher(settings)
    manifest_path = write_host_manifest(settings, launcher=launcher)
    write_host_config(settings)

    written: list[str] = []
    value = str(manifest_path)
    for browser, root in REGISTRY_ROOTS:
        if _write_registry_value(root, value):
            written.append(browser)
    if not written:
        raise ChromeBridgeError(
            "无法注册本机宿主程序",
            detail=(
                "写入注册表 HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts 失败。"
                "请确认当前用户有权限修改自己的注册表，然后重试。"
            ),
        )

    logger.info(
        "登录助手已安装：扩展 ID %s，宿主 %s，已注册 %s",
        identifier,
        launcher.name,
        ", ".join(written),
    )
    return InstallResult(
        status=bridge_status(settings),
        manifest_path=manifest_path,
        launcher=launcher,
        registered_browsers=tuple(written),
        extension_dir=extension_dir(),
    )


def uninstall_bridge(settings: Settings) -> tuple[str, ...]:
    """Remove the registry entries and the generated files.

    Only what this module created is removed: the user's browser profile, its
    cookies and the managed sessions are untouched.
    """

    removed: list[str] = []
    for browser, root in REGISTRY_ROOTS:
        if _delete_registry_key(root):
            removed.append(browser)

    directory = host_dir(settings)
    for path in (
        host_manifest_path(settings),
        host_config_path(settings),
        directory / LAUNCHER_SCRIPT_NAME,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("删除 %s 失败：%s", path.name, type(exc).__name__)

    # The host copy is removed as well - but only when this module is what put
    # it there. In a portable layout (and in a checkout) that folder *is* the
    # build output, and deleting it would throw away a developer's build.
    installed = native_host_exe(settings)
    if installed.is_file() and installed != bundled_host_executable():
        _remove_tree(installed.parent, boundary=directory)
    # An install interrupted half-way leaves this behind; it is ours either way.
    _remove_tree(_staging_dir(installed.parent), boundary=directory)

    logger.info("登录助手已卸载：%s", ", ".join(removed) or "无注册表项")
    return tuple(removed)


def repair_registration(settings: Settings) -> tuple[str, ...]:
    """Re-point the registry at our manifest when an upgrade dropped it.

    Inno Setup uninstalls the previous version before installing the new one,
    and that uninstaller removes the keys this module wrote (the installer's
    ``[Registry]`` entries carry ``uninsdeletekey``). The manifest and the host
    survive - they live in the data root, which the installer never touches - so
    without this the bridge would quietly fall back to "not installed" after
    every upgrade.

    Deliberately conservative: it only ever *re-points* an installation that
    already exists. With no manifest on disk there is nothing to repair and the
    registry is not read at all, so a machine that never installed the helper
    stays untouched.

    Returns the browsers whose entry was restored.
    """

    manifest = host_manifest_path(settings)
    if not manifest.is_file():
        return ()

    expected = str(manifest)
    stale = {root for _browser, root in REGISTRY_ROOTS if registered_path(root) != expected}
    if not stale:
        return ()

    repaired: list[str] = []
    for browser, root in REGISTRY_ROOTS:
        if root in stale and _write_registry_value(root, expected):
            repaired.append(browser)
    if repaired:
        logger.info("已修复登录助手注册：%s", ", ".join(repaired))
    return tuple(repaired)


def verify_host(settings: Settings, *, timeout: float = 25.0) -> tuple[bool, str]:
    """Run the host's ``--selftest`` so a broken launcher is caught here.

    A silent failure inside Chrome is nearly impossible to diagnose, so the
    application proves the launcher works before telling the user to try it.
    """

    launcher = launcher_path(settings)
    if not launcher.is_file():
        return False, "宿主启动器不存在，请重新安装登录助手。"

    if launcher.suffix.lower() == ".exe":
        command = [str(launcher), "--selftest"]
    else:
        command = ["cmd", "/c", str(launcher), "--selftest"]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, _SESSION_DIR_ENV: str(session_dir(settings))},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"无法启动宿主程序：{type(exc).__name__}"

    stdout = completed.stdout.decode("utf-8", "replace").strip()
    line = next((item for item in reversed(stdout.splitlines()) if item.strip()), "")
    if not line:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        return (
            False,
            f"宿主程序没有返回结果。{detail[:200]}" if detail else "宿主程序没有返回结果。",
        )
    try:
        report = json.loads(line)
    except ValueError:
        return False, f"宿主程序返回了无法解析的结果：{line[:120]}"
    if not isinstance(report, dict) or not report.get("ok"):
        return False, str(report.get("error") if isinstance(report, dict) else line)[:200]
    return True, str(report.get("sessionDir") or "")


def open_extensions_page(browser: str) -> bool:
    """Open ``chrome://extensions`` in ``browser`` itself.

    ``chrome://`` URLs cannot go through the shell - the default browser has to
    be the one that renders them - so the executable is started directly.
    """

    executable = browser_executable(browser)
    if executable is None:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - a fixed path plus a constant URL
            [str(executable), EXTENSIONS_PAGE],
            close_fds=True,
        )
    except OSError:
        logger.warning("无法启动 %s 打开扩展页面", browser)
        return False
    return True


def open_directory(path: Path) -> bool:
    """Reveal ``path`` in the shell's file manager."""

    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:  # pragma: no cover - the feature targets Windows
            subprocess.Popen(["xdg-open", str(path)], close_fds=True)  # noqa: S603,S607
    except OSError:
        logger.warning("无法打开目录：%s", path.name)
        return False
    return True


def install_hint() -> str:
    """The step-by-step the GUI shows for the one-time extension install."""

    return (
        "1. 点下面的「安装登录助手」，本程序会登记本机宿主程序\n"
        "2. 在打开的 chrome://extensions 页面右上角打开「开发者模式」\n"
        "3. 点「加载已解压的扩展程序」，选择本程序打开的 chrome-extension 文件夹\n"
        "4. 登录哔哩哔哩 / Instagram 后，点浏览器工具栏上的扩展图标，"
        "选择平台并点「发送到 Video Downloader」\n"
        "5. 回到本窗口点「我已登录，检测会话」\n\n"
        "扩展只需要安装一次。它只申请读取这两个域名的 Cookie 权限，"
        "Chrome 会自己完成解密，本程序不接触任何加密密钥。\n\n"
        "提示：Chrome 用 CreateProcess 启动本机宿主程序，所以宿主必须是可执行文件。"
        "安装包会自带 VideoDownloaderNativeHost；在源码目录里请先运行 "
        "packaging/build-native-host.ps1 生成它。"
    )
