"""Frozen-build path resolution and first-run .env bootstrap."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from config import settings as settings_module

SCANNER_PATH = Path(__file__).resolve().parent.parent / "packaging" / "scan_secrets.py"
PORTABLE_PATH = Path(__file__).resolve().parent.parent / "packaging" / "make_portable_zip.py"
SYNC_VERSION_PATH = Path(__file__).resolve().parent.parent / "packaging" / "sync_version.py"
BUNDLE_CHECK_PATH = Path(__file__).resolve().parent.parent / "packaging" / "check_bundle.py"


def _load_scanner():
    """Load packaging/scan_secrets.py without importing a 'packaging' package."""

    spec = importlib.util.spec_from_file_location("scan_secrets_under_test", SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_portable():
    """Load packaging/make_portable_zip.py the same way."""

    spec = importlib.util.spec_from_file_location("make_portable_zip_under_test", PORTABLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sync_version():
    """Load packaging/sync_version.py the same way."""

    spec = importlib.util.spec_from_file_location("sync_version_under_test", SYNC_VERSION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bundle_check():
    """Load packaging/check_bundle.py the same way."""

    spec = importlib.util.spec_from_file_location("check_bundle_under_test", BUNDLE_CHECK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load_scanner()
portable = _load_portable()
sync_version = _load_sync_version()
bundle_check = _load_bundle_check()

# --- app root ---------------------------------------------------------------


def test_app_root_is_project_root_when_not_frozen(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = settings_module._app_root()
    assert root == Path(settings_module.__file__).resolve().parent.parent


def test_app_root_follows_the_executable_when_frozen(monkeypatch, tmp_path: Path) -> None:
    exe = tmp_path / "VideoDownloader" / "VideoDownloader.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert settings_module._app_root() == exe.parent.resolve()


# --- data root --------------------------------------------------------------


def test_unfrozen_always_uses_the_app_root(tmp_path: Path) -> None:
    assert settings_module.resolve_data_root(tmp_path, frozen=False) == tmp_path


def test_frozen_uses_the_app_root_when_writable(tmp_path: Path) -> None:
    assert settings_module.resolve_data_root(tmp_path, frozen=True) == tmp_path


def test_frozen_falls_back_to_local_app_data(monkeypatch, tmp_path: Path) -> None:
    app_root = tmp_path / "Program Files" / "VideoDownloader"
    app_root.mkdir(parents=True)
    local_app_data = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(settings_module, "is_writable", lambda _path: False)

    resolved = settings_module.resolve_data_root(
        app_root, frozen=True, local_app_data=local_app_data
    )
    assert resolved == local_app_data / "VideoDownloader"
    assert resolved.is_dir(), "the fallback directory must be created"


def test_is_writable_detects_a_writable_directory(tmp_path: Path) -> None:
    assert settings_module.is_writable(tmp_path) is True
    # The probe file must not be left behind.
    assert list(tmp_path.iterdir()) == []


def test_data_root_matches_app_root_in_a_dev_checkout() -> None:
    assert settings_module.DATA_ROOT == settings_module.APP_ROOT
    assert settings_module.PROJECT_ROOT == settings_module.APP_ROOT


def test_relative_paths_resolve_against_the_data_root(settings) -> None:
    assert settings.resolve_path(Path("downloads")) == settings_module.DATA_ROOT / "downloads"
    absolute = Path("C:/absolute/place").resolve()
    assert settings.resolve_path(absolute) == absolute


# --- first-run .env ---------------------------------------------------------


def test_env_file_is_created_from_the_template(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text("HTTP_PROXY=\nYTDLP_COOKIEFILE=\n", encoding="utf-8")

    created = settings_module.ensure_env_file(tmp_path)

    assert created == tmp_path / ".env"
    assert created is not None
    # Byte-for-byte copy of the template: no credential is ever invented.
    assert created.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")


def test_existing_env_file_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("HTTP_PROXY=\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("HTTP_PROXY=http://127.0.0.1:8090\n", encoding="utf-8")

    assert settings_module.ensure_env_file(tmp_path) is None
    assert env.read_text(encoding="utf-8") == "HTTP_PROXY=http://127.0.0.1:8090\n"


def test_no_template_means_no_env(tmp_path: Path) -> None:
    assert settings_module.ensure_env_file(tmp_path) is None
    assert not (tmp_path / ".env").exists()


def test_env_creation_never_raises_on_an_unwritable_location(tmp_path: Path, monkeypatch) -> None:
    """C:\\Program Files is not writable - startup must not depend on it."""

    (tmp_path / ".env.example").write_text("HTTP_PROXY=\n", encoding="utf-8")
    monkeypatch.setattr(
        settings_module.shutil,
        "copyfile",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "拒绝访问")),
    )

    assert settings_module.ensure_env_file(tmp_path) is None


def test_template_can_come_from_a_different_directory(tmp_path: Path) -> None:
    """Install layout: template next to the exe, .env in the data root."""

    template_dir = tmp_path / "程序目录"
    target_dir = tmp_path / "数据目录"
    template_dir.mkdir()
    target_dir.mkdir()
    (template_dir / ".env.example").write_text("OUTPUT_DIR=downloads\n", encoding="utf-8")

    created = settings_module.ensure_env_file(target_dir, template_dir=template_dir)

    assert created == target_dir / ".env"
    assert created is not None
    assert created.read_text(encoding="utf-8") == "OUTPUT_DIR=downloads\n"


def test_env_file_candidates_cover_both_locations(tmp_path: Path) -> None:
    """Both locations are always read, data root last (highest precedence)."""

    app_root = tmp_path / "程序目录"
    data_root = tmp_path / "数据目录"
    assert settings_module.env_file_candidates(app_root, data_root) == (
        app_root / ".env",
        data_root / ".env",
    )


def test_env_file_candidates_deduplicate_a_portable_layout(tmp_path: Path) -> None:
    assert settings_module.env_file_candidates(tmp_path, tmp_path) == (tmp_path / ".env",)


def test_ensure_env_file_does_not_shadow_an_existing_file(tmp_path: Path, monkeypatch) -> None:
    """An .env in the other location must not be shadowed by an empty copy."""

    app_root = tmp_path / "程序目录"
    data_root = tmp_path / "数据目录"
    app_root.mkdir()
    data_root.mkdir()
    (app_root / ".env.example").write_text("HTTP_PROXY=\n", encoding="utf-8")
    (app_root / ".env").write_text("HTTP_PROXY=http://127.0.0.1:8090\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "APP_ROOT", app_root)
    monkeypatch.setattr(settings_module, "DATA_ROOT", data_root)
    monkeypatch.setattr(settings_module, "ENV_FILES", (app_root / ".env", data_root / ".env"))

    assert settings_module.ensure_env_file() is None
    assert not (data_root / ".env").exists()


# --- external .env in a frozen install --------------------------------------

FROZEN_PROBE = textwrap.dedent(
    """
    import json, os, pathlib, sys
    sys.frozen = True
    sys.executable = str(pathlib.Path(os.environ["VD_FAKE_APP"]) / "VideoDownloader.exe")
    os.environ["LOCALAPPDATA"] = os.environ["VD_LOCALAPPDATA"]
    from config.settings import ENV_FILES, get_settings
    settings = get_settings()
    print("PROBE:" + json.dumps({
        "env_files": [str(p) for p in ENV_FILES],
        "http_proxy": settings.http_proxy,
        "https_proxy": settings.https_proxy,
        "proxy": settings.proxy,
        "env_http_proxy": os.environ.get("HTTP_PROXY"),
        "env_https_proxy": os.environ.get("HTTPS_PROXY"),
        "bypass": sorted(settings.proxy_bypass_set),
    }))
    """
)


def _run_frozen_probe(
    tmp_path: Path,
    *,
    app_env: str | None,
    data_env: str | None,
    app_writable: bool,
) -> dict:
    """Run the probe with a simulated install layout.

    ``app_writable=False`` emulates an install under ``C:\\Program Files``.
    """

    local_app_data = tmp_path / "LocalAppData"
    data_dir = local_app_data / "VideoDownloader"
    data_dir.mkdir(parents=True)
    if data_env is not None:
        (data_dir / ".env").write_text(data_env, encoding="utf-8")

    if app_writable:
        app_dir = tmp_path / "AppDir"
        app_dir.mkdir()
    else:
        # A *file* used as the app directory guarantees is_writable() is False,
        # which is what makes the runtime pick the %LOCALAPPDATA% fallback.
        app_dir = tmp_path / "app-dir-is-a-file"
        app_dir.write_text("x", encoding="utf-8")
    if app_env is not None:
        (app_dir / ".env").write_text(app_env, encoding="utf-8")

    environment = {
        **os.environ,
        "VD_FAKE_APP": str(app_dir),
        "VD_LOCALAPPDATA": str(local_app_data),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in settings_module.PROXY_ENV_KEYS:
        environment.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-c", FROZEN_PROBE],
        env=environment,
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("PROBE:"))
    return json.loads(line.removeprefix("PROBE:"))


def test_frozen_build_reads_proxy_from_the_local_app_data_env(tmp_path: Path) -> None:
    """The installed layout: .env lives in %LOCALAPPDATA%, proxy must load."""

    payload = _run_frozen_probe(
        tmp_path,
        app_env=None,
        data_env=(
            "HTTP_PROXY=http://127.0.0.1:8090\n"
            "HTTPS_PROXY=http://127.0.0.1:8090\n"
            "PROXY_BYPASS_PLATFORMS=bilibili\n"
        ),
        app_writable=False,
    )

    assert payload["proxy"] == "http://127.0.0.1:8090"
    assert payload["http_proxy"] == "http://127.0.0.1:8090"
    assert payload["https_proxy"] == "http://127.0.0.1:8090"
    # Requirement: the loaded value is applied to the process environment too.
    assert payload["env_http_proxy"] == "http://127.0.0.1:8090"
    assert payload["env_https_proxy"] == "http://127.0.0.1:8090"
    # Bilibili keeps its direct route.
    assert payload["bypass"] == ["bilibili"]


def test_frozen_build_still_reads_an_env_next_to_the_executable(tmp_path: Path) -> None:
    """Portable layout keeps working (no regression)."""

    payload = _run_frozen_probe(
        tmp_path,
        app_env="HTTP_PROXY=http://127.0.0.1:9999\n",
        data_env=None,
        app_writable=True,
    )

    assert payload["proxy"] == "http://127.0.0.1:9999"


def test_missing_env_everywhere_means_no_proxy(tmp_path: Path) -> None:
    payload = _run_frozen_probe(tmp_path, app_env=None, data_env=None, app_writable=False)

    assert payload["proxy"] is None
    assert payload["env_http_proxy"] is None


def test_later_env_file_takes_precedence(tmp_path: Path) -> None:
    """Pydantic semantics the two-location lookup relies on."""

    from pydantic_settings import BaseSettings, SettingsConfigDict

    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text("HTTP_PROXY=http://from-app-dir:1\n", encoding="utf-8")
    second.write_text("HTTP_PROXY=http://from-data-root:2\n", encoding="utf-8")

    class Probe(BaseSettings):
        model_config = SettingsConfigDict(env_file=(first, second), extra="ignore")
        http_proxy: str | None = None

    assert Probe().http_proxy == "http://from-data-root:2"


# --- proxy environment export -----------------------------------------------


@pytest.fixture
def proxy_env():
    """Give a test a proxy-free environment and clean up what it exports.

    ``apply_proxy_environment`` mutates ``os.environ`` on purpose, so a test
    that calls it must not leak those variables into the rest of the session.
    """

    original = {key: os.environ.get(key) for key in settings_module.PROXY_ENV_KEYS}
    settings_module._exported_proxy_keys.clear()
    for key in settings_module.PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    settings_module._exported_proxy_keys.clear()
    for key in settings_module.PROXY_ENV_KEYS:
        os.environ.pop(key, None)
        if original[key] is not None:
            os.environ[key] = original[key]


def test_apply_proxy_environment_sets_all_casings(settings, proxy_env) -> None:
    settings.http_proxy = "http://127.0.0.1:8090"

    changed = settings_module.apply_proxy_environment(settings)

    assert "HTTP_PROXY" in changed
    assert "HTTPS_PROXY" in changed
    # Windows environment variables are case-insensitive, so one assignment
    # satisfies both spellings that libraries look for.
    for key in settings_module.PROXY_ENV_KEYS:
        assert os.environ[key] == "http://127.0.0.1:8090"


def test_apply_proxy_environment_overwrites_a_stale_value(settings, proxy_env) -> None:
    """The configuration being saved is authoritative for this process."""

    os.environ["HTTP_PROXY"] = "http://stale:1"
    settings.http_proxy = "http://127.0.0.1:8090"

    settings_module.apply_proxy_environment(settings)

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:8090"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:8090"


def test_disabling_the_proxy_clears_what_the_app_set(settings, proxy_env) -> None:
    """Switching the proxy off must not keep using the old one."""

    settings.http_proxy = "http://127.0.0.1:8090"
    settings.https_proxy = "http://127.0.0.1:8090"
    settings_module.apply_proxy_environment(settings)
    assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:8090"

    settings.http_proxy = None
    settings.https_proxy = None
    changed = settings_module.apply_proxy_environment(settings)

    assert "HTTP_PROXY" in changed
    assert os.environ.get("HTTP_PROXY") is None
    assert os.environ.get("HTTPS_PROXY") is None


def test_a_variable_the_app_never_set_is_left_alone(settings, proxy_env) -> None:
    os.environ["HTTP_PROXY"] = "http://user-exported:1"
    settings.http_proxy = None
    settings.https_proxy = None

    assert settings_module.apply_proxy_environment(settings) == []
    assert os.environ["HTTP_PROXY"] == "http://user-exported:1"


def test_direct_client_disables_trust_env(settings, monkeypatch) -> None:
    """Bilibili must stay direct even with HTTP_PROXY in the environment."""

    from core import http as http_module

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(http_module.httpx, "AsyncClient", _FakeClient)
    settings.http_proxy = "http://127.0.0.1:8090"

    http_module.build_client(settings, use_proxy=False)
    assert captured["proxy"] is None
    assert captured["trust_env"] is False

    captured.clear()
    http_module.build_client(settings, use_proxy=True)
    assert captured["proxy"] == "http://127.0.0.1:8090"
    assert captured["trust_env"] is True


@pytest.mark.parametrize(
    "placeholder",
    ["YOUTUBE_API_KEY=", "INSTAGRAM_ACCESS_TOKEN=", "BILIBILI_SESSDATA="],
)
def test_generated_env_only_carries_empty_placeholders(tmp_path: Path, placeholder: str) -> None:
    template = tmp_path / ".env.example"
    template.write_text(f"{placeholder}\nHTTP_PROXY=\n", encoding="utf-8")

    created = settings_module.ensure_env_file(tmp_path)

    assert created is not None
    text = created.read_text(encoding="utf-8")
    assert "=" in text
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            assert line.endswith("="), f"placeholder must stay empty: {line}"


# --- sensitive information scan ---------------------------------------------


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist" / "VideoDownloader"
    (dist / "_internal").mkdir(parents=True)
    (dist / "VideoDownloader.exe").write_bytes(b"MZ fake exe")
    (dist / ".env.example").write_text("HTTP_PROXY=\n", encoding="utf-8")
    return dist


def test_clean_distribution_passes(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    failures, notes = scan.build_report(dist, project)
    assert failures == []
    assert any("8090" in note for note in notes)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "cookies.txt",
        "instagram_cookies.txt",
        "www.bilibili.com_cookies.txt",
    ],
)
def test_structural_scan_rejects_credentials_files(tmp_path: Path, name: str) -> None:
    """A browser export is ``<domain>_cookies.txt`` as often as ``cookies.txt``."""

    dist = _fake_dist(tmp_path)
    (dist / name).write_text("secret", encoding="utf-8")
    failures = scan.structural_violations(dist)
    assert any(name in failure for failure in failures)


@pytest.mark.parametrize("name", [".ENV", "Cookies.TXT", "Instagram_Cookies.TXT"])
def test_the_structural_scan_folds_the_case_of_a_filename(tmp_path: Path, name: str) -> None:
    """Windows packages ``.ENV`` and ``.env`` as the very same file.

    A case-sensitive name check therefore reports a clean tree while the
    installer packs a real credential file.
    """

    dist = _fake_dist(tmp_path)
    (dist / name).write_text("secret", encoding="utf-8")

    assert scan.structural_violations(dist) != []


def test_the_structural_scan_folds_the_case_of_a_directory(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "Secrets").mkdir()

    assert scan.structural_violations(dist) != []


def test_a_session_that_escaped_its_folder_is_still_rejected(tmp_path: Path) -> None:
    """The name rule has to work at any depth, not only at the top level."""

    dist = _fake_dist(tmp_path)
    nested = dist / "chrome-extension" / "icons"
    nested.mkdir(parents=True)
    (nested / "instagram_cookies.txt").write_text("sessionid=x", encoding="utf-8")

    failures = scan.structural_violations(dist)
    assert any("instagram_cookies.txt" in failure for failure in failures)


def test_the_installer_excludes_every_cookie_export() -> None:
    """The staging scan and the installer must agree on what may ship.

    ``installer.iss`` copies ``{#SourceDir}\\*`` recursively, so any name it does
    not exclude is a name that reaches the user's disk - and no single wildcard
    covers ``cookies.txt`` and ``instagram_cookies.txt`` at once.
    """

    script = (Path(__file__).resolve().parent.parent / "packaging" / "installer.iss").read_text(
        encoding="utf-8"
    )
    excludes = next(line for line in script.splitlines() if "Excludes:" in line)

    for pattern in (
        ".env",
        "secrets",
        "cookies.txt",
        "*_cookies.txt",
        "downloads",
        "logs",
        "*.db",
        "*.log",
        "*.part",
    ):
        assert pattern in excludes, f"安装包 Excludes 缺少 {pattern}"


def _build_script() -> str:
    """``build-installer.ps1`` as text, BOM stripped."""

    return (
        Path(__file__).resolve().parent.parent / "packaging" / "build-installer.ps1"
    ).read_text(encoding="utf-8-sig")


def _installer_forbidden_filter() -> str:
    """The ``Where-Object`` body that rejects staging files, read from the script.

    Taken out of ``build-installer.ps1`` itself rather than copied, so the test
    exercises the rules that actually run instead of a duplicate that can drift.
    """

    script = _build_script()
    start = script.index("Where-Object {") + len("Where-Object {")
    end = script.index("\n        }", start)
    return "\n".join(
        line for line in script[start:end].splitlines() if not line.strip().startswith("#")
    )


def test_the_installer_script_rejects_cookie_exports_like_the_scanner(tmp_path: Path) -> None:
    """The inline check is the last gate before Inno Setup copies the folder.

    It runs straight after ``scan_secrets.py``, and the point of a second gate is
    that it still holds when the first one is skipped (``-SkipScan``). A
    ``<domain>_cookies.txt`` the scanner rejects therefore has to be rejected
    here too - whatever the case of its name, because Windows does not
    distinguish them.
    """

    interpreter = shutil.which("powershell") or shutil.which("pwsh")
    assert interpreter, "构建脚本是 PowerShell，这个测试需要它的解释器"

    # Two folders, not one: Windows cannot hold ``.env`` and ``.ENV`` at the same
    # time - they are one and the same file there, which is precisely the trap
    # the check has to survive. Each folder therefore gets a set of names that
    # are distinct from each other whatever their case.
    spellings = {
        "lower": [
            ".env",
            "cookies.txt",
            "instagram_cookies.txt",
            "www.bilibili.com_cookies.txt",
            "downloads.db",
            "downloader.log",
        ],
        "mixed": [
            ".ENV",
            "Cookies.TXT",
            "Instagram_Cookies.TXT",
            "WWW.BILIBILI.COM_COOKIES.TXT",
            "Downloads.DB",
            "Downloader.LOG",
        ],
    }

    body = _installer_forbidden_filter()
    for label, names in spellings.items():
        staging = tmp_path / label / "VideoDownloader"
        staging.mkdir(parents=True)
        for name in names:
            (staging / name).write_text("x", encoding="utf-8")
        # The two files that have to survive, or no build could ever pass.
        (staging / "VideoDownloader.exe").write_bytes(b"MZ")
        (staging / ".env.example").write_text("HTTP_PROXY=\n", encoding="utf-8")

        probe = (
            f"$items = Get-ChildItem -LiteralPath '{staging}' -Recurse -Force\n"
            f"$items | Where-Object {{ {body} }} | ForEach-Object {{ $_.Name }}\n"
        )
        result = subprocess.run(
            [interpreter, "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        blocked = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        missing = [name for name in names if name not in blocked]
        assert not missing, f"[{label}] 内联检查漏掉了: {missing}"
        assert "VideoDownloader.exe" not in blocked, "主程序不该被拦"
        assert ".env.example" not in blocked, "模板必须放行，否则构建永远失败"


def test_structural_scan_rejects_secrets_directory(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "secrets").mkdir()
    failures = scan.structural_violations(dist)
    assert any("secrets" in failure for failure in failures)


def test_env_example_is_allowed(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    assert scan.structural_violations(dist) == []


def test_structural_scan_rejects_database_and_logs(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "downloads.db").write_bytes(b"SQLite format 3")
    (dist / "downloader.log").write_text("x", encoding="utf-8")
    failures = scan.structural_violations(dist)
    assert len(failures) == 2


def test_collect_secret_values_reads_env_and_cookies(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "secrets").mkdir(parents=True)
    (project / ".env").write_text(
        "YOUTUBE_API_KEY=AIzaSyREALKEYVALUE123\n"
        "HTTP_PROXY=http://127.0.0.1:8090\n"
        "YTDLP_COOKIEFILE=secrets/cookies.txt\n"
        "OUTPUT_DIR=downloads\n"
        "PROXY_BYPASS_PLATFORMS=bilibili\n",
        encoding="utf-8",
    )
    (project / "secrets" / "cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tREALSESSIONVALUE\n",
        encoding="utf-8",
    )

    values = scan.collect_secret_values(project)
    assert b"REALSESSIONVALUE" in values
    assert b"AIzaSyREALKEYVALUE123" in values
    # Configuration is not a credential - especially the local proxy address.
    assert b"http://127.0.0.1:8090" not in values
    assert b"secrets/cookies.txt" not in values
    assert b"downloads" not in values
    assert b"bilibili" not in values
    # Empty placeholders must not be treated as credentials.
    assert b"" not in values


def test_cookie_file_paths_are_configuration_not_credentials(tmp_path: Path) -> None:
    """``*_COOKIEFILE`` holds a path; keying on the name "COOKIE" is a false positive."""

    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "YTDLP_COOKIEFILE=secrets/cookies.txt\n"
        "BILIBILI_COOKIEFILE=www.bilibili.com_cookies.txt\n"
        "BILIBILI_SESSDATA=\n",
        encoding="utf-8",
    )

    values = scan.collect_secret_values(project)

    assert b"secrets/cookies.txt" not in values
    assert b"www.bilibili.com_cookies.txt" not in values


def test_short_values_are_not_treated_as_credentials(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("YOUTUBE_API_KEY=short\n", encoding="utf-8")
    assert scan.collect_secret_values(project) == []


def test_real_secret_value_in_dist_fails_the_build(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "_internal" / "payload.bin").write_bytes(b"junk-REALSESSIONVALUE-junk")
    project = tmp_path / "project"
    (project / "secrets").mkdir(parents=True)
    (project / "secrets" / "cookies.txt").write_text(
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tREALSESSIONVALUE\n",
        encoding="utf-8",
    )

    failures, _ = scan.build_report(dist, project)
    assert any("真实凭据值" in failure for failure in failures)


def test_cookie_file_marker_in_dist_fails_the_build(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "cookies-copy.txt").write_bytes(b"# Netscape HTTP Cookie File\n")
    failures, _ = scan.build_report(dist, tmp_path / "project")
    assert any("Cookie 文件内容" in failure for failure in failures)


def test_keywords_inside_internal_are_informational_only(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "_internal" / "yt_dlp_extractor.pyc").write_bytes(b"...sessionid...csrftoken...")

    hard, soft = scan.scan_for_keywords(dist)
    assert hard == []
    assert soft and "sessionid" in {hit.decode() for hit in soft[0][1]}


def test_keywords_outside_internal_fail_the_build(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "notes.txt").write_bytes(b"my sessionid is here")

    failures, _ = scan.build_report(dist, tmp_path / "project")
    assert any("敏感关键字" in failure for failure in failures)


def test_env_example_is_exempt_from_the_keyword_check(tmp_path: Path) -> None:
    """The shipped template legitimately names the keys, with empty values."""

    dist = _fake_dist(tmp_path)
    (dist / ".env.example").write_text("BILIBILI_SESSDATA=\nYOUTUBE_API_KEY=\n", encoding="utf-8")

    failures, _ = scan.build_report(dist, tmp_path / "project")
    assert failures == []


def test_third_party_binaries_under_tools_are_informational(tmp_path: Path) -> None:
    """ffmpeg's own HTTP stack references cookie names and the Netscape format."""

    dist = _fake_dist(tmp_path)
    payload = dist / "tools" / "ffmpeg" / "bin"
    payload.mkdir(parents=True)
    (payload / "ffmpeg.exe").write_bytes(
        b"...sessionid...access_token...Netscape HTTP Cookie File..."
    )

    failures, notes = scan.build_report(dist, tmp_path / "project")
    assert failures == []
    assert any("第三方" in note for note in notes)


def test_cookie_marker_outside_third_party_still_fails(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    (dist / "leaked-cookies.txt").write_bytes(b"# Netscape HTTP Cookie File\n")
    failures, _ = scan.build_report(dist, tmp_path / "project")
    assert any("Cookie 文件内容" in failure for failure in failures)


def test_proxy_port_is_never_treated_as_a_credential(tmp_path: Path) -> None:
    """8090 is a supported local configuration, not a secret."""

    dist = _fake_dist(tmp_path)
    (dist / "README-FIRST.txt").write_bytes(
        b"HTTP_PROXY=http://127.0.0.1:8090\nHTTPS_PROXY=http://127.0.0.1:8090\n"
    )

    failures, _ = scan.build_report(dist, tmp_path / "project")
    assert failures == []
    # And the scanner never even looks for it.
    assert all(b"8090" not in keyword for keyword in scan.SENSITIVE_KEYWORDS)


def test_scanner_ignores_empty_secret_list(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    assert scan.scan_for_values(dist, []) == []


# --- the Chrome sign-in helper inside the bundle ----------------------------


def test_the_extension_may_name_the_cookies_it_handles(tmp_path: Path) -> None:
    """The shipped extension has to say ``SESSDATA`` / ``sessionid`` to work."""

    dist = _fake_dist(tmp_path)
    extension = dist / "chrome-extension"
    extension.mkdir()
    (extension / "logic.js").write_text(
        "const names = ['SESSDATA', 'sessionid', 'csrftoken'];", encoding="utf-8"
    )

    failures, _ = scan.build_report(dist, tmp_path / "project")

    assert failures == []


def test_the_compiled_host_counts_as_a_bundle_of_its_own(tmp_path: Path) -> None:
    """``native_host/`` is a second PyInstaller tree, with its own ``_internal``."""

    dist = _fake_dist(tmp_path)
    payload = dist / "native_host" / "VideoDownloaderNativeHost" / "_internal"
    payload.mkdir(parents=True)
    (payload / "yt_dlp_cookies.pyc").write_bytes(b"...sessionid...Netscape HTTP Cookie File...")

    failures, notes = scan.build_report(dist, tmp_path / "project")

    assert failures == []
    assert any("随包文件" in note for note in notes)


def test_the_keyword_exemption_still_checks_for_credentials(tmp_path: Path) -> None:
    """The value check is what actually guards secrets, and it applies everywhere."""

    dist = _fake_dist(tmp_path)
    extension = dist / "chrome-extension"
    extension.mkdir()
    (extension / "logic.js").write_text("// REALSESSIONVALUE", encoding="utf-8")
    project = tmp_path / "project"
    (project / "secrets").mkdir(parents=True)
    (project / "secrets" / "cookies.txt").write_text(
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tREALSESSIONVALUE\n", encoding="utf-8"
    )

    failures, _ = scan.build_report(dist, project)

    assert any("真实凭据值" in failure for failure in failures)


def test_the_build_script_requires_both_ffmpeg_tools() -> None:
    """The release staging step must fail instead of shipping a partial FFmpeg."""

    script = (Path(__file__).resolve().parent.parent / "packaging" / "build.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "$FfmpegTools = @('ffmpeg.exe', 'ffprobe.exe')" in script
    assert "throw \"发布必需文件缺失：$source\"" in script
    assert "throw \"发布必需文件不可用：$source" in script
    assert "Copy-Item -LiteralPath $source -Destination $FfmpegDst -Force" in script


def test_the_installer_requires_both_ffmpeg_tools() -> None:
    """The final staging check must reject either missing or empty FFmpeg file."""

    script = _build_script()
    assert "'tools\\ffmpeg\\bin\\ffmpeg.exe'" in script
    assert "'tools\\ffmpeg\\bin\\ffprobe.exe'" in script
    assert "-PathType Leaf" in script
    assert "FFmpeg 文件为空" in script


def _extract_powershell_block(text: str, start_marker: str) -> str:
    """Slice a PowerShell block out of a script, matching its braces.

    Used so the tests run the guard code that actually ships instead of a copy
    that can drift away from it. Braces inside strings are fine as long as they
    are balanced, which the scripts here keep them.
    """

    start = text.index(start_marker)
    depth = 0
    for index in range(text.index("{", start), len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"花括号不配对：{start_marker!r}")


def _run_powershell(probe: str) -> subprocess.CompletedProcess[str]:
    interpreter = shutil.which("powershell") or shutil.which("pwsh")
    assert interpreter, "构建脚本是 PowerShell，这个测试需要它的解释器"
    return subprocess.run(
        [interpreter, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _build_script_text() -> str:
    return (Path(__file__).resolve().parent.parent / "packaging" / "build.ps1").read_text(
        encoding="utf-8-sig"
    )


def _native_host_script_text() -> str:
    return (
        Path(__file__).resolve().parent.parent / "packaging" / "build-native-host.ps1"
    ).read_text(encoding="utf-8-sig")


# --- build directory isolation and existing-artifact protection -------------


def test_the_native_host_script_can_isolate_its_work_directory() -> None:
    """Redirecting -OutputDir must not clean the project's build\\native_host.

    The host build used to hard-code its PyInstaller workpath to
    ``<project>\\build\\native_host`` and delete it unconditionally, so a build
    aimed at a temporary directory still wiped the project's own intermediates.
    """

    script = _native_host_script_text()

    assert "[string]$WorkDir" in script
    assert "$PSBoundParameters.ContainsKey('OutputDir')" in script
    assert "'vd-native-host-work'" in script, "重定向输出时必须把工作目录移出项目"
    assert "$WorkDir = Join-Path $ProjectRoot 'build\\native_host'" in script, "默认值应保持不变"
    assert "Assert-InsideProject" not in script, "旧守卫会拒绝 %TEMP% 下的隔离目录"
    assert "Assert-SafeToRemove $dir" in script


def test_the_build_script_passes_an_explicit_work_dir_to_the_host_build() -> None:
    """The host's workpath must stay out of the directory that gets packaged."""

    script = _build_script_text()

    assert "$NativeHostWork = Join-Path $BuildDir 'native_host'" in script
    assert "& $NativeHostScript -OutputDir $NativeHostOut -WorkDir $NativeHostWork" in script


def test_the_build_script_can_build_into_a_temporary_output_root() -> None:
    """-OutputRoot is the isolation escape hatch the cleanup guard points at."""

    script = _build_script_text()

    assert "[string]$OutputRoot" in script
    # The default layout must stay exactly what build-installer.ps1 expects.
    assert "$DistDir = Join-Path $ProjectRoot 'dist'" in script
    assert "$BuildDir = Join-Path $ProjectRoot 'build'" in script
    assert "$DistDir = Join-Path $resolvedRoot 'dist'" in script
    assert "$BuildDir = Join-Path $resolvedRoot 'build'" in script


def test_the_build_script_refuses_to_clean_existing_artifacts() -> None:
    """A non-empty dist\\ or build\\ stops the build unless it is confirmed."""

    script = _build_script_text()

    assert "[switch]$OverwriteExisting" in script
    assert "目标目录已有内容，未获确认，拒绝删除" in script
    assert "Get-ChildItem -LiteralPath $dir -Force" in script
    assert "Remove-BuildDirectory $dir" in script
    assert "Remove-ProjectDirectory" not in script


def test_the_installer_refuses_to_clean_an_existing_output_directory() -> None:
    """The previous release's Setup.exe must survive an unconfirmed rebuild."""

    script = _build_script()
    assert "[switch]$OverwriteExisting" in script
    assert "安装包输出目录已有内容，未获确认，拒绝删除" in script
    assert "Get-ChildItem -LiteralPath $OutDir -Force" in script


def test_the_removal_guard_rejects_directories_that_are_never_safe(tmp_path: Path) -> None:
    """Run the shipped guard: deep paths pass, roots and well-known dirs do not."""

    guard = _extract_powershell_block(_build_script_text(), "function Assert-SafeToRemove")
    safe = tmp_path / "vd-guard" / "dist"
    safe.mkdir(parents=True)
    temp_root = os.environ.get("TEMP", str(tmp_path))

    probe = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProjectRoot = 'C:\\definitely-not-the-real-project-root'\n"
        + guard
        + "\n"
        + f"foreach ($candidate in @('{safe}', 'C:\\', '{temp_root}')) {{\n"
        + "    try {\n"
        + "        Assert-SafeToRemove $candidate\n"
        + "        Write-Output \"ACCEPT $candidate\"\n"
        + "    }\n"
        + "    catch {\n"
        + "        Write-Output \"REJECT $candidate\"\n"
        + "    }\n"
        + "}\n"
    )
    result = _run_powershell(probe)
    assert result.returncode == 0, result.stderr
    verdicts = {
        line.split()[1]: line.split()[0] for line in result.stdout.splitlines() if line.strip()
    }
    assert verdicts.get(str(safe)) == "ACCEPT", f"深层临时目录应可删除：{verdicts}"
    assert verdicts.get("C:\\") == "REJECT", f"盘根必须拒绝：{verdicts}"
    assert verdicts.get(temp_root) == "REJECT", f"%TEMP% 本身必须拒绝：{verdicts}"


def test_the_cleanup_guard_stops_on_a_non_empty_directory(tmp_path: Path) -> None:
    """Behaviour of the real step-3 cleanup: empty passes, non-empty needs consent."""

    text = _build_script_text()
    guard = _extract_powershell_block(text, "function Assert-SafeToRemove")
    remover = _extract_powershell_block(text, "function Remove-BuildDirectory")
    cleanup = _extract_powershell_block(text, "foreach ($dir in @($BuildDir, $DistDir))")
    missing = tmp_path / "no-such-dist"

    def run(build_dir: Path, overwrite: bool) -> str:
        probe = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProjectRoot = 'C:\\definitely-not-the-real-project-root'\n"
            + guard
            + "\n"
            + remover
            + "\n"
            + f"$BuildDir = '{build_dir}'\n$DistDir = '{missing}'\n"
            + f"$OverwriteExisting = ${str(overwrite).lower()}\n"
            + "try {\n"
            + "\n".join("    " + line if line.strip() else line for line in cleanup.splitlines())
            + "\n    Write-Output 'CLEANUP=PASS'\n"
            + "}\n"
            + "catch {\n"
            + "    Write-Output 'CLEANUP=REFUSED'\n"
            + "}\n"
        )
        result = _run_powershell(probe)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    empty = tmp_path / "empty-build"
    empty.mkdir()
    assert "CLEANUP=PASS" in run(empty, overwrite=False), "空目录不应阻止构建"
    assert empty.is_dir(), "空目录本来就该留着"

    occupied = tmp_path / "occupied-build"
    occupied.mkdir()
    (occupied / "previous-artifact.bin").write_bytes(b"old release")
    assert "CLEANUP=REFUSED" in run(occupied, overwrite=False), "非空目录必须停下来提示"
    assert (occupied / "previous-artifact.bin").is_file(), "未确认时不得删除已有产物"

    assert "CLEANUP=PASS" in run(occupied, overwrite=True), "确认后应可清理"
    assert not occupied.exists(), "确认后目录应被移除"


def test_the_build_script_ships_the_login_helper() -> None:
    """The host must land in the folder ``core.chrome_bridge`` looks in.

    ``build.ps1`` writes it under ``dist/VideoDownloader/native_host`` while the
    application looks for ``APP_ROOT/native_host/VideoDownloaderNativeHost``. If
    the two ever drift apart the release silently loses its sign-in helper, and
    nothing fails until a user tries to log in.
    """

    from core import chrome_bridge

    script = (Path(__file__).resolve().parent.parent / "packaging" / "build.ps1").read_text(
        encoding="utf-8"
    )

    assert f"$NativeHostOut = Join-Path $AppDir '{chrome_bridge.HOST_DIRNAME}'" in script
    assert f"$ExtensionDst = Join-Path $AppDir '{chrome_bridge.EXTENSION_DIRNAME}'" in script
    assert "& $NativeHostScript -OutputDir $NativeHostOut" in script
    assert chrome_bridge.HOST_FOLDER_NAME in script
    assert chrome_bridge.PACKAGED_HOST_NAME in script


def test_the_host_selftest_cannot_leave_a_secrets_folder_in_dist() -> None:
    """The host's self-test really creates the session directory it reports.

    Under a frozen build that directory resolves relative to the executable, so
    without an override the build drops a ``secrets/`` folder inside
    ``dist/VideoDownloader/native_host/VideoDownloaderNativeHost`` - which the
    packaging scan (correctly) rejects, and which also blocks the Portable ZIP.
    ``build.yml`` overrides the variable for its own self-test call; the script
    that builds the host has to do the same, or every build stops at step 6.
    """

    script = (
        Path(__file__).resolve().parent.parent / "packaging" / "build-native-host.ps1"
    ).read_text(encoding="utf-8")

    assert script.index("VIDEO_DOWNLOADER_SESSION_DIR") < script.index("--selftest"), (
        "必须在自检之前覆盖会话目录"
    )
    assert script.count("VIDEO_DOWNLOADER_SESSION_DIR") >= 2, "覆盖之后必须还原"


def test_the_build_script_does_not_depend_on_the_callers_directory() -> None:
    """``uv sync`` resolves the project from the *current* directory."""

    script = (Path(__file__).resolve().parent.parent / "packaging" / "build.ps1").read_text(
        encoding="utf-8"
    )
    sync = script.index("'sync', '--dev'")

    assert "Push-Location $ProjectRoot" in script[:sync]
    assert "Pop-Location" in script[sync:]


def test_the_whole_chain_stages_the_same_directory() -> None:
    """build.ps1 -> dist/VideoDownloader -> installer.iss must agree.

    The installer copies ``{#SourceDir}\\*`` into ``{app}``, and at run time
    ``APP_ROOT`` *is* ``{app}`` - so the directory build.ps1 assembles is exactly
    the one the application will look in for its extension and its host. Moving
    either end breaks the helper without breaking the build.
    """

    packaging = Path(__file__).resolve().parent.parent / "packaging"
    build = (packaging / "build.ps1").read_text(encoding="utf-8")
    installer = (packaging / "build-installer.ps1").read_text(encoding="utf-8")
    script = (packaging / "installer.iss").read_text(encoding="utf-8")

    assert "$DistDir = Join-Path $ProjectRoot 'dist'" in build
    assert "$AppDir = Join-Path $DistDir 'VideoDownloader'" in build
    assert "$AppDir = Join-Path $ProjectRoot 'dist\\VideoDownloader'" in installer
    assert '"/DSourceDir=$AppDir"' in installer
    assert 'Source: "{#SourceDir}\\*"; DestDir: "{app}";' in script


def test_the_installer_declares_the_same_version_as_the_project() -> None:
    """Setup.exe must not claim a version the application never heard of.

    ``installer.iss`` carries two numbers: ``AppVersion`` (what the wizard and
    the uninstall entry show) and ``VersionInfoVersion`` (the dotted quad
    Windows stamps onto Setup.exe, and the one an upgrade compares). The two
    drifted apart for eight releases - Setup.exe reported 1.1.0.0 while the
    product was 1.09 - so both are pinned to ``pyproject.toml`` here.
    """

    root = Path(__file__).resolve().parent.parent
    version = sync_version.read_pyproject((root / "pyproject.toml").read_text(encoding="utf-8"))
    installer = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")

    assert sync_version.read_installer(installer) == version
    assert sync_version.read_installer_versioninfo(installer) == sync_version.version_info_dotted(
        version
    )


def test_the_version_gate_covers_every_declared_location() -> None:
    """``--check`` is the release gate, so it has to see all five numbers."""

    root = Path(__file__).resolve().parent.parent
    version = sync_version.read_pyproject((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert sync_version.ISS_VERSIONINFO_KEY in sync_version.snapshot()
    assert sync_version.check(version) == []


def test_a_stale_version_info_version_is_caught() -> None:
    """The drift this release fixed must fail ``--check`` if it returns."""

    text = '#define AppVersion "1.11"\nVersionInfoVersion=1.1.0.0\n'

    assert sync_version.read_installer(text) == "1.11"
    assert sync_version.read_installer_versioninfo(text) == "1.1.0.0"
    assert sync_version.version_info_dotted("1.11") == "1.11.0.0"

    rewritten = sync_version.write_installer(text, "1.11")
    assert "VersionInfoVersion=1.11.0.0" in rewritten
    assert '#define AppVersion "1.11"' in rewritten


# --- what the portable archive must (and must not) contain -------------------


def test_the_archive_requires_the_login_helper() -> None:
    """A release without the helper cannot sign in on Chrome or Edge."""

    root = portable.ARCHIVE_ROOT
    required = portable.REQUIRED_ENTRIES
    host = f"{root}/native_host/VideoDownloaderNativeHost/VideoDownloaderNativeHost.exe"

    assert f"{root}/VideoDownloader.exe" in required
    assert f"{root}/chrome-extension/manifest.json" in required
    assert f"{root}/chrome-extension/logic.js" in required
    assert host in required


def test_the_build_leaves_out_exactly_the_development_files() -> None:
    """The archive check and the staging step must name the same files.

    ``verify()`` rejects a ZIP containing a development file, so ``build.ps1``
    has to be the one that leaves them behind - if the two lists drift, every
    release fails at the very last step.
    """

    build = (Path(__file__).resolve().parent.parent / "packaging" / "build.ps1").read_text(
        encoding="utf-8"
    )
    for name in portable.DEV_ONLY_EXTENSION_FILES:
        assert f"'{name}'" in build, f"build.ps1 没有排除 {name}"


# --- installer artefact scan ------------------------------------------------


def test_single_file_scan_is_documented_as_best_effort(tmp_path: Path) -> None:
    artefact = tmp_path / "VideoDownloader-Setup.exe"
    artefact.write_bytes(b"MZ installer payload")
    project = tmp_path / "project"
    project.mkdir()

    failures, notes = scan.scan_single_file(artefact, project)
    assert failures == []
    assert any("压缩" in note for note in notes)


def test_single_file_scan_catches_an_embedded_cookie_file(tmp_path: Path) -> None:
    artefact = tmp_path / "VideoDownloader-Setup.exe"
    artefact.write_bytes(b"MZ...# Netscape HTTP Cookie File\n...")
    project = tmp_path / "project"
    project.mkdir()

    failures, _ = scan.scan_single_file(artefact, project)
    assert any("Cookie 文件标记" in failure for failure in failures)


def test_single_file_scan_catches_a_real_credential(tmp_path: Path) -> None:
    artefact = tmp_path / "VideoDownloader-Setup.exe"
    artefact.write_bytes(b"MZ...REALSESSIONVALUE...")
    project = tmp_path / "project"
    (project / "secrets").mkdir(parents=True)
    (project / "secrets" / "cookies.txt").write_text(
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tREALSESSIONVALUE\n", encoding="utf-8"
    )

    failures, _ = scan.scan_single_file(artefact, project)
    assert any("真实凭据值" in failure for failure in failures)


def test_single_file_scan_reports_a_missing_artefact(tmp_path: Path) -> None:
    failures, _ = scan.scan_single_file(tmp_path / "nope.exe", tmp_path)
    assert failures and "找不到文件" in failures[0]


# --- bundled system DLLs (the v1.31 Qt crash) -------------------------------
#
# On 2026-10-16 the release build ran in a shell whose PATH also held a Poppler
# runtime, so PyInstaller resolved Qt6Core's ``icuuc.dll`` import to Poppler's
# ICU 78 and copied it into ``_internal\``. There it shadowed the Windows ICU
# shim Qt 6.11 links against, and every frozen build died with
# ``ImportError: DLL load failed while importing QtCore`` - before logging was
# configured, i.e. with no log to look at. These tests pin the two defences:
# the spec's provenance filter and the post-build ``check_bundle.py`` gate.


def _spec_text() -> str:
    spec = Path(__file__).resolve().parent.parent / "packaging" / "VideoDownloader.spec"
    return spec.read_text(encoding="utf-8")


def test_the_spec_filters_binaries_by_provenance() -> None:
    """The build must not bundle a DLL that came from outside the venv."""

    spec = _spec_text()

    assert "def _drop_foreign_binaries(" in spec
    assert "def _trusted_binary_roots(" in spec
    # Applied to a.binaries *before* the EXE/COLLECT consume it.
    assert "a.binaries, _foreign_binaries = _drop_foreign_binaries(list(a.binaries))" in spec
    assert spec.index("_drop_foreign_binaries(list(a.binaries))") < spec.index("pyz = PYZ(")


def test_the_spec_trusts_the_virtualenv_and_the_interpreter() -> None:
    spec = _spec_text()
    assert "sys.prefix" in spec
    assert "sys.base_prefix" in spec


def test_the_bundle_check_flags_a_shadowing_system_dll(tmp_path: Path) -> None:
    """``_internal\\icuuc.dll`` is exactly what broke Qt - it must fail the build."""

    dist = tmp_path / "dist" / "VideoDownloader"
    internal = dist / "_internal"
    internal.mkdir(parents=True)
    (dist / "VideoDownloader.exe").write_bytes(b"MZ")
    (internal / "icuuc.dll").write_bytes(b"poppler icu 78")

    failures, _ = bundle_check.build_report(dist, None)

    assert any("icuuc.dll" in failure for failure in failures)


def test_the_bundle_check_flags_ucrt_and_api_set_stubs(tmp_path: Path) -> None:
    dist = tmp_path / "dist" / "VideoDownloader"
    internal = dist / "_internal"
    internal.mkdir(parents=True)
    (dist / "VideoDownloader.exe").write_bytes(b"MZ")
    for name in ("ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "icudt78.dll"):
        (internal / name).write_bytes(b"x")

    failures, _ = bundle_check.build_report(dist, None)

    for name in ("ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "icudt78.dll"):
        assert any(name in failure for failure in failures), name


def test_the_bundle_check_leaves_a_wheels_private_runtime_alone(tmp_path: Path) -> None:
    """PySide6 vendors MSVCP140/VCRUNTIME140 in its own folder on purpose."""

    dist = tmp_path / "dist" / "VideoDownloader"
    internal = dist / "_internal"
    internal.mkdir(parents=True)
    (dist / "VideoDownloader.exe").write_bytes(b"MZ")
    (internal / "PySide6").mkdir()
    (internal / "PySide6" / "MSVCP140.dll").write_bytes(b"wheel payload")
    (internal / "shiboken6").mkdir()
    (internal / "shiboken6" / "VCRUNTIME140.dll").write_bytes(b"wheel payload")

    failures, notes = bundle_check.build_report(dist, None)

    assert failures == []
    assert any("未发现" in note for note in notes)


def test_the_bundle_check_accepts_a_clean_payload(tmp_path: Path) -> None:
    dist = tmp_path / "dist" / "VideoDownloader"
    internal = dist / "_internal"
    (internal / "PySide6").mkdir(parents=True)
    (dist / "VideoDownloader.exe").write_bytes(b"MZ")
    (internal / "PySide6" / "Qt6Core.dll").write_bytes(b"qt")

    failures, _ = bundle_check.build_report(dist, None)
    assert failures == []


def test_the_bundle_check_rejects_a_foreign_toc_source(tmp_path: Path) -> None:
    """Provenance comes from the TOC, and it is the general detector."""

    dist = tmp_path / "dist" / "VideoDownloader"
    (dist / "_internal").mkdir(parents=True)
    (dist / "VideoDownloader.exe").write_bytes(b"MZ")

    build = tmp_path / "build"
    build.mkdir()
    polluted = (
        r"C:\\Users\\someone\\.cache\\codex-runtimes"
        r"\\poppler\\Library\\bin\\icuuc.dll"
    )
    (build / "Analysis-00.toc").write_text(
        "a.binaries = [\n"
        "('icuuc.dll',\n"
        f" '{polluted}',\n"
        " 'BINARY'),\n"
        "('PySide6\\\\Qt6Core.dll',\n"
        " r'C:\\\\project\\\\.venv\\\\Lib\\\\site-packages\\\\PySide6\\\\Qt6Core.dll',\n"
        " 'BINARY'),\n"
        "]\n",
        encoding="utf-8",
    )

    sources = bundle_check.toc_sources(build)
    offenders = bundle_check.foreign_sources(sources, bundle_check.trusted_roots())

    assert any(name == "icuuc.dll" for name, _src in offenders)
    assert all(name != "qt6core.dll" for name, _src in offenders)


def test_the_bundle_check_reports_a_missing_payload(tmp_path: Path) -> None:
    failures, _ = bundle_check.build_report(tmp_path / "dist" / "VideoDownloader", None)
    assert failures and "找不到载荷目录" in failures[0]


def test_the_build_script_scrubs_dll_pollution_from_path() -> None:
    """A foreign runtime on PATH is what caused the whole incident."""

    script = _build_script_text()

    assert "function Remove-DllPollutionFromPath" in script
    assert "$DllPollutionMarkers = @('codex-runtimes', 'poppler', 'libheif')" in script
    # Called before PyInstaller starts, not after.
    scrub = script.index("Remove-DllPollutionFromPath\n")
    pyinstaller = script.index("$pyiArgs = @(")
    assert scrub < pyinstaller
    # Only the process PATH is touched - never the user's persisted one.
    assert "$env:PATH = ($kept -join ';')" in script
    assert "SetEnvironmentVariable" not in script


def test_the_build_script_runs_the_bundle_check() -> None:
    """The second gate must run in build.ps1, after the sensitive scan."""

    script = _build_script_text()

    assert "$BundleCheck = Join-Path $PackagingDir 'check_bundle.py'" in script
    assert "'--build', $BuildDir" in script
    assert "打包产物检查" in script


def test_the_installer_script_runs_the_bundle_check() -> None:
    """And again right before Inno Setup packs the folder."""

    script = _build_script()
    assert "$BundleCheck = Join-Path $PackagingDir 'check_bundle.py'" in script
    assert "$BundleCheck, '--dist', $AppDir" in script


def test_the_bundle_check_cannot_be_skipped_by_the_secret_scan_switch() -> None:
    """``-SkipScan`` skips the *secret* scan; the DLL gate is not optional."""

    script = _build_script()
    invocation = script.index("$BundleCheck, '--dist', $AppDir")
    # The gate call sits outside the ``if ($SkipScan) { ... } else { ... }``
    # block, so ``-SkipScan`` cannot silence it.
    assert script.index("if ($SkipScan)") < invocation
    before = script[:invocation]
    assert before.count("}") >= before.count("{"), "调用必须落在 else 块之外"


# --- upgrade leftovers ------------------------------------------------------
#
# Inno Setup's ``[Files]`` only *adds and overwrites*; it never deletes a file
# that a previous version installed but the new payload no longer ships. v1.31
# proved the cost: a broken package whose ``_internal\`` carried an
# incompatible ICU 78 was replaced by a fixed one, yet the upgrade left 88
# orphaned DLLs behind - and a leftover ``_internal\icuuc.dll`` shadows the OS
# shim, so Qt still refused to load and the app died with no log. The
# ``[InstallDelete]`` section is the cure, and these tests keep it honest.


def _installer_text() -> str:
    """``installer.iss`` as text."""

    return (
        Path(__file__).resolve().parent.parent / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")


def _installer_section(name: str) -> list[str]:
    """The non-comment directive lines of one Inno Setup ``[section]``.

    A real section parser, not a ``split``: the prose *above* an entry may
    mention other sections by name (ours does), so the only reliable signal is
    a line whose first character is ``[``.
    """

    directives: list[str] = []
    in_section = False
    for raw in _installer_text().splitlines():
        stripped = raw.strip()
        if stripped.startswith("["):
            in_section = stripped.casefold() == f"[{name.casefold()}]"
            continue
        if not in_section or not stripped or stripped.startswith(";"):
            continue
        directives.append(stripped)
    return directives


def _install_delete_lines() -> list[str]:
    """The ``[InstallDelete]`` entries, comments and blanks stripped.

    Parsed out of the real file so the tests see exactly what Inno Setup will
    run rather than a hand-copied duplicate that can drift.
    """

    return _installer_section("InstallDelete")


def _inno_wildcard_to_regex(pattern: str) -> str:
    """Translate an Inno ``Name:`` wildcard to a regex ``^...$``.

    Only ``*`` and ``?`` are meaningful here; both are escaped first so a
    literal ``.`` in ``icuuc.dll`` stays literal.
    """

    import re as _re

    out = _re.escape(pattern)
    out = out.replace(r"\*", ".*").replace(r"\?", ".")
    return "^" + out + "$"


def _install_delete_names() -> list[tuple[str, str]]:
    """``(directory, filename)`` for every ``Type: files`` entry.

    Raises if the section holds something the app did not author, so a future
    edit that quietly widens the scope fails loudly instead of shipping.
    """

    entries: list[tuple[str, str]] = []
    for line in _install_delete_lines():
        assert line.casefold().startswith("type: files;"), f"只允许 Type: files 条目: {line}"
        name = line.split("Name:", 1)[1].strip().strip('"')
        assert name.startswith("{app}\\"), f"清理必须限定在 {{app}} 内: {line}"
        assert "\\_internal\\" in name, f"只清理 _internal 目录: {line}"
        directory, filename = name.rsplit("\\", 1)
        entries.append((directory, filename))
    return entries


def test_the_upgrade_cleanup_covers_every_forbidden_dll() -> None:
    """Each pattern the bundle check rejects must also be cleaned on upgrade.

    Both directions matter, so both are asserted:

    * every cleanup rule names a *specific* forbidden pattern - never a broad
      ``*.dll`` that could delete something the payload needs;
    * every forbidden pattern has a rule that covers it in each bundle - a
      missing rule is precisely the v1.31 bug (a leftover that survives and
      shadows the OS copy).

    Sourced from ``check_bundle.FORBIDDEN_DLL_PATTERNS`` so the two lists
    cannot drift apart silently.
    """

    import re as _re

    expected = {
        _re.escape(pattern).replace(r"\*", ".*")
        for pattern in bundle_check.FORBIDDEN_DLL_PATTERNS
    }
    rules = {filename for _directory, filename in _install_delete_names()}

    # (1) No rule is broader than the forbidden set.
    for filename in rules:
        assert any(_re.fullmatch(pat, filename.casefold()) for pat in expected), (
            f"清理规则 {filename} 不对应任何禁用名（可能误删 payload 需要的文件）"
        )

    # (2) Every forbidden pattern has a covering rule in *each* bundle folder.
    directories = {directory for directory, _ in _install_delete_names()}
    assert directories, "没有任何清理规则"
    for pattern in bundle_check.FORBIDDEN_DLL_PATTERNS:
        wanted = pattern.casefold()
        # ``icu*.dll`` is covered by the narrower explicit rules; treat a
        # pattern as satisfied when a rule matches it *or* it is a superset of
        # a rule (the narrower rule does the work).
        covered = any(
            _re.fullmatch(_inno_wildcard_to_regex(filename), wanted)
            or _re.fullmatch(_re.escape(wanted).replace(r"\*", ".*"), filename.casefold())
            for filename in rules
        )
        assert covered, f"禁用名 {pattern} 没有任何 InstallDelete 规则覆盖"
    assert len(directories) == 2, f"两个 payload 目录都应各有一份规则，实为 {directories}"


def test_the_upgrade_cleanup_targets_both_payload_folders() -> None:
    """The main bundle and the native host bundle both carry ``_internal``.

    Neither may be left out: the native host is a second ``--onedir`` tree with
    its own ``_internal``, and it is the process Chrome starts - a poisoned copy
    there breaks sign-in just as surely.
    """

    by_directory: dict[str, set[str]] = {}
    for directory, filename in _install_delete_names():
        by_directory.setdefault(directory.casefold(), set()).add(filename.casefold())

    main = "{app}\\_internal".casefold()
    host = "{app}\\native_host\\videodownloadernativehost\\_internal".casefold()

    assert main in by_directory, "主 bundle 的 _internal 没有被清理"
    assert host in by_directory, "原生消息宿主也有一份 _internal，必须一并清理"
    # Same rules for both, so neither bundle can quietly fall behind.
    assert by_directory[main] == by_directory[host], "两个 _internal 的清理规则必须一致"


def test_the_upgrade_cleanup_never_deletes_what_the_payload_ships() -> None:
    """The safety invariant: no cleanup rule may match a file we actually ship.

    Proven the same way for both bundles: build a *representative* payload (the
    files the build legitimately produces, plus a couple of names the forbidden
    list must never hit) and assert no cleanup rule touches any of them. Doing
    it against a synthetic tree rather than the on-disk ``dist/`` keeps the
    test meaningful whatever state a developer's working copy is in - the same
    reason ``check_bundle`` is exercised with ``tmp_path`` fixtures.
    """

    import fnmatch

    # Names the payload really contains: the executable, the Qt/ICU-free
    # runtime files, and - critically - files whose names merely *resemble* the
    # forbidden set without matching it.
    keep = (
        "VideoDownloader.exe",
        "PySide6/Qt6Core.dll",
        "PySide6/MSVCP140.dll",
        "python312.dll",
        "libcrypto-3-x64.dll",  # Python's own OpenSSL - must survive
        "icu.dll",  # the OS shim's name is *not* on the forbidden list
        "ucrtbased.dll",  # the debug CRT is a different name
        "api-ms-win-crt-runtime-l1-1-0.deps.json",  # not a .dll
    )

    for _directory, filename in _install_delete_names():
        for relative in keep:
            leaf = relative.rsplit("/", 1)[-1]
            assert not fnmatch.fnmatch(leaf.casefold(), filename.casefold()), (
                f"清理规则 {filename} 会命中产物自带的 {relative}"
            )


def test_the_upgrade_cleanup_never_touches_user_data_or_the_uninstaller() -> None:
    """Scoped to the app folder, and blind to everything we must preserve."""

    directives = _install_delete_lines()
    body = "\n".join(directives).casefold()

    # Never a per-user or system location.
    assert "localappdata" not in body
    assert "%" not in body
    assert "{app}" in body

    # Never the files that must survive an upgrade.
    for protected in (".env", "unins"):
        assert protected not in body, f"清理段不该清理 {protected}"

    # And every entry is a plain ``Type: files`` - no directory nukes.
    for line in directives:
        assert "filesandordirs" not in line.casefold()
