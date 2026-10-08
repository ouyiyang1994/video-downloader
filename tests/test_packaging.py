"""Frozen-build path resolution and first-run .env bootstrap."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from config import settings as settings_module

SCANNER_PATH = Path(__file__).resolve().parent.parent / "packaging" / "scan_secrets.py"
PORTABLE_PATH = Path(__file__).resolve().parent.parent / "packaging" / "make_portable_zip.py"


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


scan = _load_scanner()
portable = _load_portable()

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


@pytest.mark.parametrize("name", [".env", "cookies.txt"])
def test_structural_scan_rejects_credentials_files(tmp_path: Path, name: str) -> None:
    dist = _fake_dist(tmp_path)
    (dist / name).write_text("secret", encoding="utf-8")
    failures = scan.structural_violations(dist)
    assert any(name in failure for failure in failures)


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
