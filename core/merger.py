"""ffmpeg discovery and audio/video muxing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from config.settings import PROJECT_ROOT, Settings
from core.exceptions import MergeError

logger = logging.getLogger(__name__)

_MERGE_TIMEOUT_SECONDS = 900


def locate_ffmpeg(settings: Settings | None = None) -> Path | None:
    """Find ``ffmpeg`` without requiring it on PATH.

    Search order: the ``FFMPEG_PATH`` setting, the project's vendored copy,
    then whatever is on PATH.
    """

    if settings and settings.ffmpeg_path:
        candidate = Path(settings.ffmpeg_path)
        if candidate.is_dir():
            candidate = candidate / "bin" / "ffmpeg.exe"
        if candidate.exists():
            return candidate

    vendored = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if vendored.exists():
        return vendored

    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def locate_ffprobe(settings: Settings | None = None) -> Path | None:
    ffmpeg = locate_ffmpeg(settings)
    if ffmpeg is not None:
        name = "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
        ffprobe = ffmpeg.with_name(name)
        if ffprobe.exists():
            return ffprobe
    found = shutil.which("ffprobe")
    return Path(found) if found else None


class Merger:
    """Muxes a separate video and audio stream into a single container."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.ffmpeg_path = locate_ffmpeg(settings)

    @property
    def available(self) -> bool:
        return self.ffmpeg_path is not None

    async def merge(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        *,
        container: str = "mp4",
    ) -> Path:
        if not self.ffmpeg_path:
            raise MergeError(
                "未找到 ffmpeg，无法合并音视频",
                detail="请把 ffmpeg 放到 tools/ffmpeg/bin/ 或设置 FFMPEG_PATH",
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
        ]
        if container == "mp4":
            command += ["-movflags", "+faststart"]
        command.append(str(output_path))

        logger.debug("ffmpeg 合并：%s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_MERGE_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:  # pragma: no cover - machine dependent
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise MergeError("ffmpeg 合并超时", detail=str(exc)) from exc

        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise MergeError("ffmpeg 合并失败", detail=message[-500:] or "unknown error")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise MergeError("ffmpeg 未生成输出文件", detail=str(output_path))

        return output_path

    async def duration(self, path: Path) -> float | None:
        """Return media duration in seconds using ffprobe, if available."""

        ffprobe = locate_ffprobe(self.settings)
        if ffprobe is None:
            return None
        command = [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        with contextlib.suppress(ValueError):
            return float(stdout.decode().strip())
        return None
