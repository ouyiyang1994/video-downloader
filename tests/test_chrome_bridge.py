"""Tests for the Chrome extension bridge and the native messaging host.

Everything here is offline: the registry is faked, so a test can never touch the
developer's real ``HKCU``, and the generated files all land in ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from config import settings as settings_module
from config.settings import Settings
from core import chrome_bridge, native_host
from core.exceptions import ChromeBridgeError
from core.models import Platform
from platforms.bilibili.adapter import BilibiliAdapter
from platforms.instagram.adapter import InstagramAdapter

# --- a registry that exists only in memory -----------------------------------


class _FakeKey:
    def __init__(self, registry: FakeRegistry, path: str) -> None:
        self._registry = registry
        self._path = path

    def __enter__(self) -> _FakeKey:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeRegistry:
    """The subset of ``winreg`` this project uses, backed by a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 0x20006
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes = 0

    # -- the API chrome_bridge actually calls -------------------------------
    def CreateKeyEx(self, root: object, path: str, reserved: int, access: int) -> _FakeKey:
        self.values.setdefault(path, "")
        return _FakeKey(self, path)

    def OpenKey(self, root: object, path: str) -> _FakeKey:
        if path not in self.values:
            raise FileNotFoundError(2, "no such key", path)
        return _FakeKey(self, path)

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[str, int]:
        return self.values[key._path], self.REG_SZ

    def SetValueEx(self, key: _FakeKey, name: str, reserved: int, kind: int, value: str) -> None:
        self.values[key._path] = value
        self.writes += 1

    def DeleteKey(self, root: object, path: str) -> None:
        if path not in self.values:
            raise FileNotFoundError(2, "no such key", path)
        del self.values[path]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry()
    monkeypatch.setattr(chrome_bridge, "winreg", fake)
    return fake


@pytest.fixture(autouse=True)
def no_bundled_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic about the *shipped* host.

    ``install_bridge`` provisions the compiled host out of the application
    folder whenever it finds one. On a developer machine that folder really
    exists - ``packaging/build-native-host.ps1`` put it there - so without this
    the suite would silently change what it exercises, and pay a ~40 MB copy per
    test. Tests that care point the function at their own folder.
    """

    monkeypatch.setattr(
        chrome_bridge, "bundled_host_executable", lambda: Path("__no_bundled_host__")
    )


@pytest.fixture(autouse=True)
def sandboxed_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep the real data root out of reach for every test in this module.

    ``native_host._settings()`` falls back to ``Settings()`` whenever nothing
    injects a root, and ``Settings.resolve_path`` resolves relative paths
    against ``config.settings.DATA_ROOT`` - which on a developer machine *is*
    the project folder, so the host's ``store`` action used to write straight
    into the real ``secrets/``.

    Pointing ``DATA_ROOT`` at ``tmp_path`` makes that fallback harmless, so a
    test that forgets to inject its own settings still cannot touch the
    developer's data. ``APP_ROOT`` is deliberately left alone: it is the real
    folder, and the regression tests below use it as the thing to compare
    against.
    """

    root = tmp_path / "data-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings_module, "DATA_ROOT", root)
    return root


def real_secrets_snapshot() -> dict[str, str]:
    """Name -> content digest of the *real* ``secrets/`` folder.

    Empty when the folder does not exist, so a clean checkout is covered too.
    Only digests are kept; no cookie value is ever read into an assertion.
    """

    directory = settings_module.APP_ROOT / "secrets"
    if not directory.is_dir():
        return {}
    return {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(directory.iterdir())
        if item.is_file()
    }


def _fake_bundled_host(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in for ``native_host/VideoDownloaderNativeHost`` as built."""

    folder = root / "app" / chrome_bridge.HOST_DIRNAME / chrome_bridge.HOST_FOLDER_NAME
    folder.mkdir(parents=True)
    executable = folder / chrome_bridge.PACKAGED_HOST_NAME
    executable.write_bytes(b"MZ fake host")
    # The host reads its own config from this folder, so the whole --onedir
    # tree has to travel - not just the executable.
    (folder / chrome_bridge.HOST_CONFIG_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(chrome_bridge, "bundled_host_executable", lambda: executable)
    return executable


# --- the extension manifest --------------------------------------------------


def test_the_bundled_extension_is_present_and_parsable() -> None:
    manifest = chrome_bridge.read_extension_manifest()
    assert manifest["manifest_version"] == 3
    assert manifest["name"]
    assert manifest["version"]


def test_extension_id_is_derived_from_the_pinned_key() -> None:
    """A pinned key is what makes ``allowed_origins`` knowable in advance."""

    assert chrome_bridge.extension_id() == "deegmfcjldojkppoahflbkikhppnfepd"


def test_extension_id_matches_chromes_algorithm() -> None:
    import base64
    import hashlib

    key = chrome_bridge.read_extension_manifest()["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    expected = digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    assert chrome_bridge.extension_id() == expected


def test_a_broken_key_is_reported_not_guessed() -> None:
    with pytest.raises(ChromeBridgeError):
        chrome_bridge.extension_id_from_key("!!!not base64!!!")
    with pytest.raises(ChromeBridgeError):
        chrome_bridge.extension_id_from_key("")


def test_a_missing_extension_folder_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ChromeBridgeError) as info:
        chrome_bridge.read_extension_manifest(tmp_path)
    assert "扩展" in info.value.message


def test_the_extension_asks_only_for_the_two_platform_domains() -> None:
    """The cookie permission is scoped by host_permissions, so this is a
    security boundary and not a style choice."""

    manifest = chrome_bridge.read_extension_manifest()
    hosts = manifest["host_permissions"]
    assert hosts
    for host in hosts:
        # The two cookie domains, plus the loopback fallback transport - which
        # grants no cookie access at all.
        assert "bilibili.com" in host or "instagram.com" in host or host == "http://127.0.0.1/*", (
            host
        )
    joined = " ".join(hosts)
    for broad in ("<all_urls>", "*://*/*", "http://*/*", "https://*/*", "http://localhost/*"):
        assert broad not in joined


def test_the_loopback_permission_cannot_reach_a_cookie_domain() -> None:
    """Only 127.0.0.1, and only over http - nothing routable."""

    loopback = [
        host
        for host in chrome_bridge.read_extension_manifest()["host_permissions"]
        if "127.0.0.1" in host
    ]
    assert loopback == ["http://127.0.0.1/*"]


def test_the_extension_asks_only_for_the_permissions_it_needs() -> None:
    permissions = set(chrome_bridge.read_extension_manifest()["permissions"])
    assert permissions == {"cookies", "nativeMessaging", "activeTab"}


def test_the_extension_declares_no_content_script_or_remote_code() -> None:
    manifest = chrome_bridge.read_extension_manifest()
    assert "content_scripts" not in manifest
    assert "externally_connectable" not in manifest
    assert "web_accessible_resources" not in manifest
    # The only plain-http origin may be the loopback bridge; nothing remote.
    remote = [
        host
        for host in manifest["host_permissions"]
        if host.startswith("http://") and "127.0.0.1" not in host
    ]
    assert remote == []


def test_the_host_name_is_the_same_in_all_three_places() -> None:
    """The extension, the host and the manifest must agree, or nothing works."""

    logic = (chrome_bridge.extension_dir() / "logic.js").read_text(encoding="utf-8")
    assert f"'{native_host.HOST_NAME}'" in logic
    assert native_host.HOST_NAME == chrome_bridge.HOST_NAME


# --- launcher and manifest ---------------------------------------------------


def test_launcher_points_at_the_host_module_and_the_session_dir(settings: Settings) -> None:
    source = chrome_bridge.launcher_source(settings)
    assert "core.native_host" in source
    assert native_host.SESSION_DIR_ENV in source
    assert str(chrome_bridge.session_dir(settings)) in source
    assert "%*" in source, "参数必须透传，否则 --selftest 无法运行"


def test_launcher_keeps_the_protocol_channel_clean(settings: Settings) -> None:
    """Anything printed on stdout would desynchronise Chrome's framing."""

    source = chrome_bridge.launcher_source(settings)
    assert "chcp 65001 >nul" in source
    assert "@echo off" in source


def test_writing_the_launcher_is_idempotent(settings: Settings) -> None:
    first = chrome_bridge.write_launcher(settings)
    body = first.read_bytes()
    second = chrome_bridge.write_launcher(settings)
    assert first == second
    assert second.read_bytes() == body


def test_host_manifest_allows_exactly_our_extension(settings: Settings) -> None:
    chrome_bridge.write_launcher(settings)
    path = chrome_bridge.write_host_manifest(settings)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["name"] == chrome_bridge.HOST_NAME
    assert payload["type"] == "stdio"
    assert payload["allowed_origins"] == [f"chrome-extension://{chrome_bridge.extension_id()}/"]
    assert Path(payload["path"]).is_file()


def test_host_manifest_refuses_to_point_at_nothing(settings: Settings) -> None:
    with pytest.raises(ChromeBridgeError):
        chrome_bridge.write_host_manifest(settings, launcher=settings.native_host_dir / "nope.exe")


# --- provisioning the shipped host -------------------------------------------


def test_install_provisions_the_bundled_host(
    settings: Settings, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install under ``C:\\Program Files``: the manifest must name a real file.

    The application folder is read-only there, so the host is copied into the
    data root - which is also where its ``host-config.json`` has to live.
    """

    source = _fake_bundled_host(tmp_path, monkeypatch)

    result = chrome_bridge.install_bridge(settings)

    installed = chrome_bridge.native_host_exe(settings)
    assert installed.is_file()
    assert installed.read_bytes() == source.read_bytes()
    assert (installed.parent / chrome_bridge.HOST_CONFIG_NAME).is_file(), (
        "整个 --onedir 目录都要复制，宿主需要同目录的配置文件"
    )

    # The manifest points at the copy, and the copy exists - the two can never
    # disagree, which is what makes Chrome able to start it.
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert Path(manifest["path"]) == installed
    assert Path(manifest["path"]).is_file()
    assert chrome_bridge.bridge_status(settings).launcher_kind == "exe"
    assert chrome_bridge.bridge_status(settings).ready is True


def test_provisioning_is_skipped_when_the_copy_is_current(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-clicking 「安装 / 更新登录助手」 must not rewrite 40 MB."""

    _fake_bundled_host(tmp_path, monkeypatch)
    first = chrome_bridge.provision_host(settings)
    assert first is not None
    marker = first.parent / "keep-me"
    marker.write_text("x", encoding="utf-8")

    second = chrome_bridge.provision_host(settings)

    assert second == first
    assert marker.is_file(), "已是最新时不应重新复制"


def test_provisioning_replaces_a_stale_copy(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgrade ships a new host; the old copy must not win."""

    source = _fake_bundled_host(tmp_path, monkeypatch)
    chrome_bridge.provision_host(settings)
    installed = chrome_bridge.native_host_exe(settings)
    installed.write_bytes(b"old build")
    source.write_bytes(b"MZ the new build")

    chrome_bridge.provision_host(settings)

    assert installed.read_bytes() == b"MZ the new build"


def test_a_portable_layout_needs_no_copy(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the application folder *is* the data root, nothing is duplicated."""

    source = _fake_bundled_host(tmp_path, monkeypatch)
    monkeypatch.setattr(chrome_bridge, "native_host_exe", lambda _settings: source)

    assert chrome_bridge.provision_host(settings) == source
    assert source.is_file()


def test_no_bundled_host_means_no_provisioning(settings: Settings) -> None:
    """A plain checkout that has not built the host yet must still work."""

    assert chrome_bridge.provision_host(settings) is None


def test_a_registered_bridge_with_a_deleted_host_is_reported(
    settings: Settings, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest can outlive the host it names, and Chrome says so bluntly.

    Chrome answers a stale path with "native messaging host not found", so the
    status has to point at 「安装 / 更新登录助手」 rather than leaving the user with
    a dead end - re-installing the helper re-provisions the host.
    """

    _fake_bundled_host(tmp_path, monkeypatch)
    chrome_bridge.install_bridge(settings)
    chrome_bridge.native_host_exe(settings).unlink()  # e.g. antivirus quarantine

    status = chrome_bridge.bridge_status(settings)

    assert status.registered is True
    assert status.host_bundled is True
    assert status.host_present is False
    assert status.ready is False
    assert "尚未安装" in status.summary()
    assert "安装 / 更新登录助手" in status.advice()


def test_reinstalling_repairs_a_deleted_host(
    settings: Settings, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advice has to be actionable: one click must really fix it."""

    _fake_bundled_host(tmp_path, monkeypatch)
    chrome_bridge.install_bridge(settings)
    chrome_bridge.native_host_exe(settings).unlink()

    chrome_bridge.install_bridge(settings)

    assert chrome_bridge.bridge_status(settings).ready is True


def test_an_upgraded_application_reports_a_stale_host(
    settings: Settings, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy keeps working after an upgrade, but it is the previous build.

    Nothing refreshes it on its own - only an explicit 「安装 / 更新登录助手」
    does - so the status has to say so instead of claiming everything is current.
    """

    source = _fake_bundled_host(tmp_path, monkeypatch)
    chrome_bridge.install_bridge(settings)
    assert chrome_bridge.bridge_status(settings).host_stale is False

    source.write_bytes(b"MZ the next release")  # what an upgrade ships

    status = chrome_bridge.bridge_status(settings)

    assert status.host_present is True
    assert status.ready is True
    assert status.host_stale is True
    assert "升级前" in status.summary()
    assert "安装 / 更新登录助手" in status.advice()

    chrome_bridge.install_bridge(settings)

    assert chrome_bridge.bridge_status(settings).host_stale is False


def test_a_failed_copy_keeps_working_with_the_older_one(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chrome may be running the host right now; that must not break install."""

    source = _fake_bundled_host(tmp_path, monkeypatch)
    chrome_bridge.provision_host(settings)
    installed = chrome_bridge.native_host_exe(settings)
    source.write_bytes(b"MZ newer build")  # makes the copy look stale

    def _refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(chrome_bridge.shutil, "copytree", _refuse)

    assert chrome_bridge.provision_host(settings) == installed
    assert installed.is_file()


def test_a_failed_copy_without_a_fallback_is_reported(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_bundled_host(tmp_path, monkeypatch)

    def _refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(chrome_bridge.shutil, "copytree", _refuse)

    with pytest.raises(ChromeBridgeError) as info:
        chrome_bridge.provision_host(settings)
    assert "宿主" in info.value.message


# --- registry ----------------------------------------------------------------


def test_install_registers_every_chromium_browser(
    settings: Settings, registry: FakeRegistry
) -> None:
    result = chrome_bridge.install_bridge(settings)

    assert result.registered_browsers == tuple(
        browser for browser, _root in chrome_bridge.REGISTRY_ROOTS
    )
    for _browser, root in chrome_bridge.REGISTRY_ROOTS:
        assert registry.values[root + "\\" + chrome_bridge.HOST_NAME] == str(
            chrome_bridge.host_manifest_path(settings)
        )
    assert chrome_bridge.is_registered(settings) is True


def test_install_is_idempotent(settings: Settings, registry: FakeRegistry) -> None:
    chrome_bridge.install_bridge(settings)
    before = dict(registry.values)
    chrome_bridge.install_bridge(settings)
    assert registry.values == before


def test_uninstall_removes_only_what_it_created(settings: Settings, registry: FakeRegistry) -> None:
    chrome_bridge.install_bridge(settings)
    removed = chrome_bridge.uninstall_bridge(settings)

    assert set(removed) == {browser for browser, _root in chrome_bridge.REGISTRY_ROOTS}
    assert registry.values == {}
    assert chrome_bridge.is_registered(settings) is False
    assert not chrome_bridge.host_manifest_path(settings).exists()


def test_uninstall_leaves_the_sessions_alone(settings: Settings, registry: FakeRegistry) -> None:
    """Requirement: signing out of the helper must not delete a saved session."""

    chrome_bridge.install_bridge(settings)
    from core.session_store import make_cookie, session_file, write_session

    write_session(
        settings,
        Platform.BILIBILI,
        [make_cookie("SESSDATA", "value", domain=".bilibili.com")],
        domain_suffix="bilibili.com",
    )
    chrome_bridge.uninstall_bridge(settings)

    assert session_file(settings, Platform.BILIBILI).is_file()


def test_unregistering_something_absent_is_not_an_error(
    settings: Settings, registry: FakeRegistry
) -> None:
    assert chrome_bridge.uninstall_bridge(settings) == ()


def test_uninstall_removes_the_host_copy_it_made(
    settings: Settings, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only what this module copied is removed - never the build output."""

    source = _fake_bundled_host(tmp_path, monkeypatch)
    chrome_bridge.install_bridge(settings)
    installed = chrome_bridge.native_host_exe(settings)
    assert installed.is_file()

    chrome_bridge.uninstall_bridge(settings)

    assert not installed.parent.exists()
    assert source.is_file(), "构建产物不属于卸载范围"
    assert settings.native_host_dir.is_dir()


def test_uninstall_keeps_the_build_output_of_a_checkout(
    settings: Settings, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Portable layout: that folder *is* the build, so it must survive."""

    source = _fake_bundled_host(tmp_path, monkeypatch)
    monkeypatch.setattr(chrome_bridge, "native_host_exe", lambda _settings: source)
    chrome_bridge.install_bridge(settings)

    chrome_bridge.uninstall_bridge(settings)

    assert source.is_file()


def test_uninstall_clears_a_half_written_copy(settings: Settings, registry: FakeRegistry) -> None:
    """An interrupted install must not leave a staging folder behind."""

    chrome_bridge.install_bridge(settings)
    staging = chrome_bridge._staging_dir(chrome_bridge.native_host_exe(settings).parent)  # noqa: SLF001
    staging.mkdir(parents=True)

    chrome_bridge.uninstall_bridge(settings)

    assert not staging.exists()


# --- repairing a registration an upgrade removed ------------------------------


def test_repair_restores_a_registration_an_upgrade_removed(
    settings: Settings, registry: FakeRegistry
) -> None:
    """Inno uninstalls the previous version first, and that wipes our keys.

    The manifest and the host are in the data root, which the installer never
    touches, so the registration can simply be pointed back at them.
    """

    _install_fake_host(settings)
    chrome_bridge.install_bridge(settings)
    for _browser, root in chrome_bridge.REGISTRY_ROOTS:
        registry.DeleteKey(None, root + "\\" + chrome_bridge.HOST_NAME)
    assert chrome_bridge.is_registered(settings) is False

    repaired = chrome_bridge.repair_registration(settings)

    assert set(repaired) == {browser for browser, _root in chrome_bridge.REGISTRY_ROOTS}
    assert chrome_bridge.is_registered(settings) is True
    assert chrome_bridge.bridge_status(settings).ready is True


def test_repair_does_nothing_when_the_helper_was_never_installed(
    settings: Settings, registry: FakeRegistry
) -> None:
    """No manifest on disk means no installation to repair."""

    assert chrome_bridge.repair_registration(settings) == ()
    assert registry.values == {}


def test_repair_never_reads_the_registry_without_a_manifest(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine that never installed the helper must not be touched at all."""

    class _Forbidden:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"不应访问注册表：{name}")

    monkeypatch.setattr(chrome_bridge, "winreg", _Forbidden())

    assert chrome_bridge.repair_registration(settings) == ()


def test_repair_leaves_a_correct_registration_alone(
    settings: Settings, registry: FakeRegistry
) -> None:
    _install_fake_host(settings)
    chrome_bridge.install_bridge(settings)
    writes = registry.writes

    assert chrome_bridge.repair_registration(settings) == ()
    assert registry.writes == writes


def test_repair_repoints_a_stale_path(
    settings: Settings, registry: FakeRegistry, tmp_path: Path
) -> None:
    """A moved installation must not keep a dead manifest path registered."""

    _install_fake_host(settings)
    chrome_bridge.install_bridge(settings)
    for _browser, root in chrome_bridge.REGISTRY_ROOTS:
        registry.values[root + "\\" + chrome_bridge.HOST_NAME] = str(tmp_path / "gone.json")

    repaired = chrome_bridge.repair_registration(settings)

    assert len(repaired) == len(chrome_bridge.REGISTRY_ROOTS)
    for _browser, root in chrome_bridge.REGISTRY_ROOTS:
        assert registry.values[root + "\\" + chrome_bridge.HOST_NAME] == str(
            chrome_bridge.host_manifest_path(settings)
        )


def test_status_reports_a_missing_bridge(settings: Settings, registry: FakeRegistry) -> None:
    status = chrome_bridge.bridge_status(settings)
    assert status.extension_present is True
    assert status.registered is False
    assert status.host_present is False
    assert status.ready is False
    assert status.delivered_ever is False
    assert "尚未安装" in status.summary()


def _install_fake_host(settings: Settings) -> Path:
    """Stand in for the compiled host so ``ready`` can be reached offline."""

    executable = chrome_bridge.native_host_exe(settings)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"MZ fake")
    return executable


def test_status_becomes_ready_after_install(settings: Settings, registry: FakeRegistry) -> None:
    _install_fake_host(settings)
    chrome_bridge.install_bridge(settings)
    status = chrome_bridge.bridge_status(settings)
    assert status.ready is True
    assert status.launcher_kind == "exe"
    assert status.extension_id == chrome_bridge.extension_id()
    assert "已就绪" in status.summary()


def test_a_script_launcher_alone_is_not_ready(settings: Settings, registry: FakeRegistry) -> None:
    """Chrome cannot start a ``.bat``, so the status must not claim it can."""

    chrome_bridge.install_bridge(settings)
    status = chrome_bridge.bridge_status(settings)
    assert status.launcher_kind == "script"
    assert status.ready is False
    assert "脚本启动器" in status.summary()
    assert "build-native-host.ps1" in status.advice()


def test_status_remembers_that_the_extension_has_delivered(
    settings: Settings, registry: FakeRegistry
) -> None:
    from core.session_store import make_cookie, write_session

    _install_fake_host(settings)
    chrome_bridge.install_bridge(settings)
    write_session(
        settings,
        Platform.INSTAGRAM,
        [make_cookie("sessionid", "value", domain=".instagram.com")],
        domain_suffix="instagram.com",
    )
    status = chrome_bridge.bridge_status(settings)
    assert status.delivered_ever is True
    assert "已经收到过" in status.summary()


def test_install_refuses_when_the_registry_write_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = FakeRegistry()

    def _refuse(*args: object, **kwargs: object) -> None:
        raise OSError(5, "denied")

    monkeypatch.setattr(chrome_bridge, "winreg", broken)
    monkeypatch.setattr(broken, "CreateKeyEx", _refuse)

    with pytest.raises(ChromeBridgeError) as info:
        chrome_bridge.install_bridge(settings)
    assert "注册表" in (info.value.detail or "")


def test_registry_access_is_per_user_only() -> None:
    """An administrator prompt would make the one-time install a support call."""

    assert all(root.startswith("Software\\") for _browser, root in chrome_bridge.REGISTRY_ROOTS)


def test_the_installer_cleans_up_every_registry_root() -> None:
    """Uninstall must remove exactly our keys - no more, no fewer.

    The registration itself is written at run time by the helper (an elevated
    Setup would write into the elevating account's hive), so the installer only
    carries the clean-up entries. Those are hand-written, which is why this test
    exists: a browser added to ``REGISTRY_ROOTS`` must not be forgotten, and
    ``dontcreatekey`` must stay in place - without it Setup would create empty
    keys at install time, and Chrome reads an empty key as a broken host.
    """

    script = (Path(__file__).resolve().parent.parent / "packaging" / "installer.iss").read_text(
        encoding="utf-8"
    )

    assert f'#define NativeHostName "{chrome_bridge.HOST_NAME}"' in script
    for _browser, root in chrome_bridge.REGISTRY_ROOTS:
        assert f'Root: HKCU; Subkey: "{root}\\{{#NativeHostName}}";' in script, root
    assert script.count("dontcreatekey uninsdeletekey") == len(chrome_bridge.REGISTRY_ROOTS)


def test_the_installer_removes_the_provisioned_host_on_uninstall() -> None:
    """The copy made under %LOCALAPPDATA% would otherwise be orphaned."""

    script = (Path(__file__).resolve().parent.parent / "packaging" / "installer.iss").read_text(
        encoding="utf-8"
    )

    assert "[UninstallDelete]" in script
    assert "{localappdata}\\VideoDownloader\\native_host" in script
    # And nothing else: the user's .env / secrets/ / downloads/ must survive.
    body = script.split("[UninstallDelete]", 1)[1]
    assert body.count("Type: filesandordirs") == 1


def test_the_install_hint_states_the_two_manual_clicks() -> None:
    hint = chrome_bridge.install_hint()
    assert "开发者模式" in hint
    assert "加载已解压的扩展程序" in hint
    assert "发送到 Video Downloader" in hint


def test_the_install_hint_promises_no_password_collection() -> None:
    hint = chrome_bridge.install_hint()
    assert "密码" not in hint  # nothing here asks for one
    assert "不会上传" not in hint  # that promise lives in the dialog's own text


# --- the platform table must not drift from the adapters ---------------------


@pytest.mark.parametrize("name", sorted(native_host.PLATFORMS))
def test_host_platform_table_matches_the_adapters(name: str, settings: Settings) -> None:
    """A drift here would silently accept the wrong domain or the wrong cookie."""

    import asyncio

    import httpx

    adapters: dict[str, type] = {
        "bilibili": BilibiliAdapter,
        "instagram": InstagramAdapter,
    }

    async def _adapter() -> Any:
        async with httpx.AsyncClient() as client:
            return adapters[name](settings, client)

    instance = asyncio.run(_adapter())
    spec = native_host.PLATFORMS[name]
    assert spec["domain"] == instance.session_domain
    assert tuple(spec["auth"]) == instance.session_cookie_names


# --- the generated launcher actually runs the host ---------------------------


def test_verify_host_runs_the_launcher(settings: Settings, registry: FakeRegistry) -> None:
    """End-to-end: the ``.bat`` starts the module, which answers ``--selftest``."""

    chrome_bridge.install_bridge(settings)
    ok, detail = chrome_bridge.verify_host(settings)
    assert ok, detail
    assert detail == str(chrome_bridge.session_dir(settings))


def test_verify_host_reports_a_missing_launcher(settings: Settings) -> None:
    ok, detail = chrome_bridge.verify_host(settings)
    assert ok is False
    assert "不存在" in detail


def test_the_host_accepts_the_command_line_chrome_builds() -> None:
    """Chrome always appends the caller's origin and ``--parent-window``.

    v1.06 parsed its command line strictly, so the host died with exit code 2
    before it ever read stdin and the native channel never answered a single
    message.
    """

    origin = f"chrome-extension://{chrome_bridge.extension_id()}/"
    args, unknown = native_host.parse_arguments([origin, "--parent-window=7471384"])

    assert args.selftest is False
    assert args.origin == origin
    assert args.parent_window == "7471384"
    assert unknown == []


def test_the_host_tolerates_arguments_it_does_not_know() -> None:
    """A future Chrome release must not be able to kill the host."""

    args, unknown = native_host.parse_arguments(["--brand-new-chrome-flag=1", "extra"])

    assert args.selftest is False
    assert args.origin == "extra"
    assert unknown == ["--brand-new-chrome-flag=1"]


def test_selftest_still_works_with_chrome_arguments() -> None:
    """``--selftest`` is the one flag the application itself passes."""

    assert native_host.parse_arguments(["--selftest"])[0].selftest is True

    origin = f"chrome-extension://{chrome_bridge.extension_id()}/"
    args, unknown = native_host.parse_arguments([origin, "--parent-window=0", "--selftest"])

    assert args.selftest is True
    assert args.origin == origin
    assert args.parent_window == "0"
    assert unknown == []


def test_main_does_not_abort_on_chrome_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: no ``SystemExit(2)`` and no silent death at startup."""

    calls: list[bool] = []
    monkeypatch.setattr(native_host, "selftest", lambda: calls.append(True) or 0)

    origin = f"chrome-extension://{chrome_bridge.extension_id()}/"
    assert native_host.main([origin, "--parent-window=0", "--selftest"]) == 0
    assert calls == [True]


def test_the_preflight_uses_the_chrome_command_line() -> None:
    """``verify_host`` must not pass a bare command line Chrome never sends."""

    arguments = chrome_bridge.host_probe_arguments()

    assert arguments[0] == f"chrome-extension://{chrome_bridge.extension_id()}/"
    assert any(item.startswith("--parent-window=") for item in arguments)
    assert arguments[-1] == "--selftest"


# --- native messaging framing ------------------------------------------------


def _frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def test_a_message_survives_the_round_trip() -> None:
    import io

    buffer = io.BytesIO()
    native_host.write_message(buffer, {"action": "ping", "中文": "值"})
    buffer.seek(0)
    assert native_host.read_message(buffer) == {"action": "ping", "中文": "值"}


def test_the_length_prefix_is_little_endian() -> None:
    import io

    buffer = io.BytesIO()
    native_host.write_message(buffer, {"a": 1})
    raw = buffer.getvalue()
    assert struct.unpack("<I", raw[:4])[0] == len(raw) - 4


def test_an_empty_stream_ends_cleanly() -> None:
    import io

    assert native_host.read_message(io.BytesIO(b"")) is None


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (struct.pack("<I", 0), "长度为 0"),
        (struct.pack("<I", native_host.MAX_INBOUND_BYTES + 1), "过大"),
        (b"\x02\x00", "截断"),  # a half-written length prefix
        (struct.pack("<I", 10) + b"abc", "截断"),
        (struct.pack("<I", 3) + b"not", "JSON"),
        (struct.pack("<I", 3) + b"[1]", "JSON 对象"),
    ],
)
def test_malformed_input_is_rejected(raw: bytes, match: str) -> None:
    import io

    with pytest.raises(native_host.ProtocolError) as info:
        native_host.read_message(io.BytesIO(raw))
    assert match in str(info.value)


def test_an_oversized_response_is_replaced_not_truncated() -> None:
    """A truncated frame would desynchronise the channel."""

    import io

    buffer = io.BytesIO()
    native_host.write_message(buffer, {"blob": "x" * (native_host.MAX_OUTBOUND_BYTES + 10)})
    buffer.seek(0)
    message = native_host.read_message(buffer)
    assert message is not None
    assert message["ok"] is False
    assert "过大" in message["error"]


# --- request handling --------------------------------------------------------


def test_ping_answers_without_touching_anything(settings: Settings) -> None:
    response = native_host.handle({"action": "ping"}, settings=settings)
    assert response["ok"] is True
    assert response["host"] == native_host.HOST_NAME
    assert response["platforms"] == ["bilibili", "instagram"]
    # Only the folder name - the full path has no business crossing the bridge.
    assert "/" not in response["sessionDir"] and "\\" not in response["sessionDir"]


def test_an_unknown_action_is_answered_not_raised() -> None:
    response = native_host.handle({"action": "delete-everything"})
    assert response["ok"] is False
    assert "未知的操作" in response["error"]


def test_handle_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("exploded")

    monkeypatch.setattr(native_host, "store_cookies", _boom)
    response = native_host.handle({"action": "store"})
    assert response["ok"] is False
    assert "RuntimeError" in response["error"]


# --- storing a session -------------------------------------------------------


def _bilibili_payload() -> list[dict[str, Any]]:
    return [
        {"name": "SESSDATA", "value": "SECRET-SESSDATA", "domain": ".bilibili.com", "path": "/"},
        {"name": "bili_jct", "value": "SECRET-JCT", "domain": ".bilibili.com", "path": "/"},
    ]


def test_store_writes_a_session_the_loader_can_read(settings: Settings) -> None:
    from core.http import load_cookie_header

    response = native_host.store_cookies(settings, "bilibili", _bilibili_payload())

    assert response["ok"] is True
    assert response["saved"] == 2
    assert response["cookieNames"] == ["SESSDATA", "bili_jct"]
    assert response["file"] == "bilibili_cookies.txt"

    header = load_cookie_header(
        chrome_bridge.session_dir(settings) / "bilibili_cookies.txt",
        domain_suffix="bilibili.com",
    )
    assert header is not None and "SESSDATA=SECRET-SESSDATA" in header


def test_the_response_never_carries_a_value(settings: Settings) -> None:
    response = native_host.store_cookies(settings, "bilibili", _bilibili_payload())
    blob = json.dumps(response, ensure_ascii=False)
    assert "SECRET-SESSDATA" not in blob
    assert "SECRET-JCT" not in blob


def test_a_cookie_from_another_site_is_refused(settings: Settings) -> None:
    """Rewriting a foreign domain would send that site's cookie to Bilibili."""

    payload = _bilibili_payload()
    payload.append({"name": "SESSDATA", "value": "X", "domain": ".example.com", "path": "/"})

    response = native_host.store_cookies(settings, "bilibili", payload)
    assert response["ok"] is True

    from core.session_store import session_file

    body = session_file(settings, Platform.BILIBILI).read_text(encoding="utf-8")
    assert "example.com" not in body


def test_a_cookie_without_a_domain_is_refused(settings: Settings) -> None:
    response = native_host.store_cookies(settings, "bilibili", [{"name": "SESSDATA", "value": "X"}])
    assert response["ok"] is False


def test_a_missing_authentication_cookie_is_refused(settings: Settings) -> None:
    payload = [{"name": "csrftoken", "value": "X", "domain": ".bilibili.com", "path": "/"}]
    response = native_host.store_cookies(settings, "bilibili", payload)
    assert response["ok"] is False
    assert "SESSDATA" in response["error"]


def test_sessions_stay_isolated_between_platforms(settings: Settings) -> None:
    """Bilibili's cookies must not be accepted as an Instagram session."""

    assert native_host.store_cookies(settings, "bilibili", _bilibili_payload())["ok"] is True

    cross = native_host.store_cookies(settings, "instagram", _bilibili_payload())
    assert cross["ok"] is False

    from core.session_store import session_file

    assert session_file(settings, Platform.BILIBILI).is_file()
    assert not session_file(settings, Platform.INSTAGRAM).exists()


def test_expired_cookies_are_dropped(settings: Settings) -> None:
    payload = _bilibili_payload() + [
        {
            "name": "SESSDATA",
            "value": "STALE",
            "domain": ".bilibili.com",
            "path": "/old",
            "expirationDate": 1,
        }
    ]
    response = native_host.store_cookies(settings, "bilibili", payload)
    assert response["ok"] is True

    from core.session_store import session_file

    assert "STALE" not in session_file(settings, Platform.BILIBILI).read_text(encoding="utf-8")


def test_a_session_cookie_without_an_expiry_is_kept(settings: Settings) -> None:
    response = native_host.store_cookies(settings, "bilibili", _bilibili_payload())
    assert response["ok"] is True


@pytest.mark.parametrize("bad", [None, "cookies", 42, {"name": "x"}])
def test_a_malformed_cookie_list_is_refused(settings: Settings, bad: Any) -> None:
    response = native_host.store_cookies(settings, "bilibili", bad)
    assert response["ok"] is False


def test_an_absurd_cookie_count_is_refused(settings: Settings) -> None:
    payload = [
        {"name": f"c{index}", "value": "v", "domain": ".bilibili.com", "path": "/"}
        for index in range(native_host.MAX_COOKIES + 1)
    ]
    response = native_host.store_cookies(settings, "bilibili", payload)
    assert response["ok"] is False
    assert "过多" in response["error"]


def test_an_unknown_platform_is_refused(settings: Settings) -> None:
    response = native_host.store_cookies(settings, "tiktok", _bilibili_payload())
    assert response["ok"] is False


@pytest.mark.parametrize(
    "item",
    [
        {"name": "", "value": "v", "domain": ".bilibili.com"},
        {"name": "SESSDATA", "value": "", "domain": ".bilibili.com"},
        {"name": 1, "value": "v", "domain": ".bilibili.com"},
        "not a dict",
    ],
)
def test_unusable_cookie_entries_are_skipped(settings: Settings, item: Any) -> None:
    response = native_host.store_cookies(settings, "bilibili", [item])
    assert response["ok"] is False


def test_a_relative_path_is_normalised(settings: Settings) -> None:
    payload = [
        {
            "name": "SESSDATA",
            "value": "V",
            "domain": ".bilibili.com",
            "path": "not-absolute",
        }
    ]
    assert native_host.store_cookies(settings, "bilibili", payload)["ok"] is True
    from core.session_store import session_file

    body = session_file(settings, Platform.BILIBILI).read_text(encoding="utf-8")
    assert "\t/\t" in body


# --- the whole protocol loop -------------------------------------------------


def test_serve_answers_every_message_in_order(settings: Settings) -> None:
    import io

    stdin = io.BytesIO(
        _frame({"action": "ping"})
        + _frame({"action": "store", "platform": "bilibili", "cookies": _bilibili_payload()})
        + _frame({"action": "status"})
    )
    stdout = io.BytesIO()

    assert native_host.serve(stdin, stdout, settings=settings) == 0

    stdout.seek(0)
    replies = []
    while (message := native_host.read_message(stdout)) is not None:
        replies.append(message)

    assert [reply["ok"] for reply in replies] == [True, True, True]
    assert replies[1]["cookieNames"] == ["SESSDATA", "bili_jct"]
    assert replies[2]["sessions"] == {"bilibili": True, "instagram": False}


def test_serve_stops_on_a_protocol_error_without_crashing() -> None:
    import io

    stdin = io.BytesIO(struct.pack("<I", 0))
    stdout = io.BytesIO()

    assert native_host.serve(stdin, stdout) == 2
    stdout.seek(0)
    reply = native_host.read_message(stdout)
    assert reply is not None and reply["ok"] is False


class _Trickle:
    """A stream that hands out one byte at a time, like a slow pipe."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, count: int) -> bytes:
        chunk, self._data = self._data[:count], self._data[count:]
        return chunk


def test_serve_handles_a_message_split_across_reads(settings: Settings) -> None:
    """Chrome writes the frame and the body separately; both must be read."""

    import io

    stdout = io.BytesIO()
    stdin = _Trickle(
        _frame({"action": "store", "platform": "bilibili", "cookies": _bilibili_payload()})
    )

    assert native_host.serve(stdin, stdout, settings=settings) == 0

    stdout.seek(0)
    reply = native_host.read_message(stdout)
    assert reply is not None and reply["ok"] is True
    assert reply["cookieNames"] == ["SESSDATA", "bili_jct"]


def test_a_byte_at_a_time_read_still_yields_the_message() -> None:
    message = native_host.read_message(_Trickle(_frame({"action": "ping"})))
    assert message == {"action": "ping"}


def test_logging_never_goes_to_stdout() -> None:
    """A stray byte on stdout breaks Chrome's framing."""

    source = Path(native_host.__file__).read_text(encoding="utf-8")
    assert "print(" not in source
    assert "sys.stdout.write" not in source.split("def selftest")[0]


# --- the host must never write into the developer's real data root -----------


def _instagram_payload() -> list[dict[str, Any]]:
    return [
        {"name": "sessionid", "value": "SECRET-SESSIONID", "domain": ".instagram.com", "path": "/"}
    ]


def test_serve_writes_only_into_the_injected_directory(
    settings: Settings, sandboxed_data_root: Path
) -> None:
    """Regression: the protocol loop must forward the injected settings.

    ``serve`` used to call ``handle`` without a data root, so a ``store``
    message fell back to ``Settings()`` and overwrote the developer's real
    ``secrets/bilibili_cookies.txt`` on every test run.
    """

    import io

    before = real_secrets_snapshot()

    stdin = io.BytesIO(
        _frame({"action": "store", "platform": "bilibili", "cookies": _bilibili_payload()})
    )
    assert native_host.serve(stdin, io.BytesIO(), settings=settings) == 0

    assert real_secrets_snapshot() == before, "serve 不得触碰真实的 secrets/"
    written = settings.resolve_path(settings.session_dir) / "bilibili_cookies.txt"
    assert written.is_file(), "会话必须写进注入的临时目录"
    assert "SECRET-SESSDATA" in written.read_text(encoding="utf-8")
    assert not (sandboxed_data_root / "secrets").exists(), "注入生效时不该用回退目录"


def test_the_fallback_root_is_the_data_root_not_the_project(sandboxed_data_root: Path) -> None:
    """A forgotten ``settings=`` must still stay inside the sandboxed root.

    ``sandboxed_data_root`` points ``DATA_ROOT`` at ``tmp_path``, so this
    exercises exactly the un-injected path and proves it cannot escape into the
    project folder.
    """

    import io

    before = real_secrets_snapshot()

    stdin = io.BytesIO(
        _frame({"action": "store", "platform": "instagram", "cookies": _instagram_payload()})
    )
    assert native_host.serve(stdin, io.BytesIO()) == 0

    assert real_secrets_snapshot() == before
    assert (sandboxed_data_root / "secrets" / "instagram_cookies.txt").is_file()


def test_the_host_never_creates_a_secrets_folder_in_the_project(
    sandboxed_data_root: Path,
) -> None:
    """Every message type, un-injected: ``APP_ROOT/secrets`` must not change.

    Covers a clean checkout (folder absent before, still absent after) and a
    developer machine (folder present, file list and contents identical).
    """

    import io

    directory = settings_module.APP_ROOT / "secrets"
    existed = directory.is_dir()
    before = real_secrets_snapshot()

    frames = (
        _frame({"action": "ping"})
        + _frame({"action": "store", "platform": "bilibili", "cookies": _bilibili_payload()})
        + _frame({"action": "store", "platform": "instagram", "cookies": _instagram_payload()})
        + _frame({"action": "status"})
        + _frame({"action": "clear", "platform": "bilibili"})
    )
    assert native_host.serve(io.BytesIO(frames), io.BytesIO()) == 0

    assert directory.is_dir() is existed, "宿主不得在项目里凭空创建 secrets/"
    assert real_secrets_snapshot() == before, "真实 secrets/ 的内容与文件列表都不得变化"


def test_ping_and_status_read_the_injected_root(settings: Settings) -> None:
    """The read-only actions must not fall back to the real data root either."""

    ping = native_host.handle({"action": "ping"}, settings=settings)
    assert ping["sessionDir"] == settings.session_dir.name

    status = native_host.handle({"action": "status"}, settings=settings)
    assert status["sessions"] == {"bilibili": False, "instagram": False}
