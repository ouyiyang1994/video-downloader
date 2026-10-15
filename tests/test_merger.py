"""ffmpeg discovery and audio/video muxing.

Everything here is offline: discovery runs against temporary trees, and the
merge itself uses a stubbed subprocess, so no real ffmpeg is required (or
started) by the suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config.settings import Settings
from core import merger
from core.exceptions import MergeError
from core.merger import Merger, locate_ffmpeg, locate_ffprobe


def _fake_tools(root: Path) -> Path:
    """A temporary stand-in for the vendored ``tools/ffmpeg/bin`` payload."""

    binary = root / "tools" / "ffmpeg" / "bin"
    binary.mkdir(parents=True)
    (binary / "ffmpeg.exe").write_bytes(b"MZ fake ffmpeg")
    (binary / "ffprobe.exe").write_bytes(b"MZ fake ffprobe")
    return binary / "ffmpeg.exe"


# --- discovery ---------------------------------------------------------------


def test_ffmpeg_path_accepts_a_directory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FFMPEG_PATH`` may name the folder that holds ``bin``, as documented."""

    binary = _fake_tools(tmp_path / "configured")
    settings.ffmpeg_path = str(binary.parent.parent)
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path / "no-vendored-copy")

    assert locate_ffmpeg(settings) == binary


def test_ffmpeg_path_accepts_the_executable_itself(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _fake_tools(tmp_path / "configured")
    settings.ffmpeg_path = str(binary)
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path / "no-vendored-copy")

    assert locate_ffmpeg(settings) == binary


def test_the_vendored_copy_wins_over_the_path(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release ships ffmpeg; PATH must not override it."""

    binary = _fake_tools(tmp_path)
    settings.ffmpeg_path = None
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(merger.shutil, "which", lambda name: r"C:\somewhere\ffmpeg.exe")

    assert locate_ffmpeg(settings) == binary


def test_a_missing_ffmpeg_is_reported_as_missing(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ffmpeg_path = None
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path / "empty")
    monkeypatch.setattr(merger.shutil, "which", lambda name: None)

    assert locate_ffmpeg(settings) is None
    assert locate_ffprobe(settings) is None


def test_ffprobe_is_looked_up_next_to_ffmpeg(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _fake_tools(tmp_path)
    settings.ffmpeg_path = str(binary)
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path / "no-vendored-copy")

    assert locate_ffprobe(settings) == binary.with_name("ffprobe.exe")


# --- merging -----------------------------------------------------------------


class _Process:
    """The slice of ``asyncio.subprocess.Process`` the merger uses."""

    def __init__(self, *, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr

    def kill(self) -> None:  # pragma: no cover - only the timeout path
        self.killed = True

    async def wait(self) -> int:  # pragma: no cover - only the timeout path
        return self.returncode


@pytest.fixture
def fake_ffmpeg(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings whose ffmpeg exists as a real (empty) file."""

    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(b"MZ fake ffmpeg")
    settings.ffmpeg_path = str(binary)
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path / "no-vendored-copy")
    return settings


async def test_merging_without_ffmpeg_says_what_to_install(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ffmpeg_path = None
    monkeypatch.setattr(merger, "PROJECT_ROOT", tmp_path / "empty")
    monkeypatch.setattr(merger.shutil, "which", lambda name: None)
    instance = Merger(settings)

    assert instance.available is False
    with pytest.raises(MergeError) as excinfo:
        await instance.merge(tmp_path / "v.m4s", tmp_path / "a.m4s", tmp_path / "out.mp4")

    assert "ffmpeg" in excinfo.value.message
    assert "tools/ffmpeg/bin" in (excinfo.value.detail or "")


async def test_merge_runs_ffmpeg_with_the_expected_arguments(
    fake_ffmpeg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video, audio = tmp_path / "video.m4s", tmp_path / "audio.m4s"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    output = tmp_path / "out.mp4"
    captured: list[tuple[str, ...]] = []

    async def _spawn(*command: str, **kwargs: object) -> _Process:
        captured.append(command)
        output.write_bytes(b"merged")
        return _Process(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    result = await Merger(fake_ffmpeg).merge(video, audio, output, container="mp4")

    assert result == output
    command = captured[0]
    assert command[0] == fake_ffmpeg.ffmpeg_path
    assert list(command[command.index("-i") + 1 : command.index("-i") + 2]) == [str(video)]
    assert str(audio) in command
    assert command[command.index("-c") + 1] == "copy"
    assert "+faststart" in command, "mp4 输出应当带上 faststart"
    assert command[-1] == str(output)


async def test_a_failed_merge_reports_ffmpeg_stderr(
    fake_ffmpeg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(*command: str, **kwargs: object) -> _Process:
        return _Process(returncode=1, stderr=b"Invalid data found when processing input")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    with pytest.raises(MergeError) as excinfo:
        await Merger(fake_ffmpeg).merge(
            tmp_path / "v.m4s", tmp_path / "a.m4s", tmp_path / "out.mp4"
        )

    assert "Invalid data found" in (excinfo.value.detail or "")


async def test_a_merge_that_produces_nothing_is_reported(
    fake_ffmpeg: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(*command: str, **kwargs: object) -> _Process:
        return _Process(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    with pytest.raises(MergeError) as excinfo:
        await Merger(fake_ffmpeg).merge(
            tmp_path / "v.m4s", tmp_path / "a.m4s", tmp_path / "out.mp4"
        )

    assert "未生成输出文件" in excinfo.value.message
