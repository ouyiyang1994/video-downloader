"""Command line entry point.

Usage:
    python main.py "https://www.bilibili.com/video/BV1xx411c7mD"
    python main.py "URL" --output ./downloads
    python main.py "URL" --quality 1080p
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from config.constants import QUALITY_AUDIO_ONLY, QUALITY_PRESETS
from config.settings import PROJECT_ROOT, Settings, get_settings
from core.database import DownloadDatabase
from core.downloader import ProgressUpdate
from core.exceptions import VideoDownloaderError
from core.http import build_client
from core.logging_setup import setup_logging
from core.merger import locate_ffmpeg
from core.models import DownloadResult, DownloadStatus
from core.registry import build_registry
from core.service import DownloadService

console = Console()
error_console = Console(stderr=True)

QUALITY_CHOICES = [*QUALITY_PRESETS.keys(), QUALITY_AUDIO_ONLY]


class ProgressDisplay:
    """Renders one progress bar per stream (video / audio)."""

    def __init__(self, progress: Progress) -> None:
        self.progress = progress
        self.tasks: dict[str, TaskID] = {}

    def callback(self, kind: str) -> Callable[[ProgressUpdate], None]:
        def _update(update: ProgressUpdate) -> None:
            task_id = self.tasks.get(kind)
            if task_id is None:
                label = "视频" if kind == "video" else "音频"
                task_id = self.progress.add_task(f"[cyan]{label}", total=update.total or None)
                self.tasks[kind] = task_id
            self.progress.update(
                task_id,
                completed=update.downloaded,
                total=update.total,
                speed=f"{update.speed_bps / 1024 / 1024:.2f} MB/s" if update.speed_bps else None,
            )

        return _update


def _validate_quality(quality: str) -> str:
    normalized = quality.strip().lower()
    if normalized not in QUALITY_CHOICES:
        raise typer.BadParameter(
            f"quality 必须是 {', '.join(QUALITY_CHOICES)} 之一（收到 {quality!r}）"
        )
    return normalized


def _print_result(result: DownloadResult) -> None:
    if result.status is DownloadStatus.SKIPPED:
        console.print(f"[yellow]跳过[/yellow] {result.title} — {result.skipped_reason}")
        return
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_row("平台", result.platform.display_name)
    table.add_row("标题", result.title)
    table.add_row("画质", result.quality_label)
    if result.resolution:
        table.add_row("分辨率", result.resolution)
    if result.video_path:
        table.add_row("文件", str(result.video_path))
    if result.metadata_path:
        table.add_row("元数据", str(result.metadata_path))
    table.add_row("耗时", f"{result.elapsed_seconds:.1f}s")
    console.print(table)


async def _run_download(
    settings: Settings,
    url: str,
    quality: str,
    output_dir: Path | None,
    force: bool,
    confirm_rights: bool,
) -> int:
    database = DownloadDatabase(settings)
    database.connect()
    client = build_client(settings)
    registry = None
    try:
        registry = build_registry(settings, client)
        adapter = registry.resolve(url)
        if adapter.requires_rights_confirmation and not confirm_rights:
            error_console.print(
                f"[red]{adapter.describe()} 的媒体需要通过非官方公开接口获取。[/red]\n"
                "请确认你有权下载该内容，然后追加 --confirm-rights 重新运行。\n"
                "（官方 API 路径：配置 .env 中的凭据后即可免除该确认）"
            )
            return 4

        service = DownloadService(settings, registry, client, database)
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        ) as progress:
            display = ProgressDisplay(progress)
            result = await service.download(
                url,
                quality=quality,
                output_dir=output_dir,
                force=force,
                progress={
                    "video": display.callback("video"),
                    "audio": display.callback("audio"),
                },
            )
        _print_result(result)
        return 0
    finally:
        if registry is not None:
            for extra in registry.owned_clients:
                await extra.aclose()
        await client.aclose()
        database.close()


def _run_download_sync(*args: Any, **kwargs: Any) -> int:
    return asyncio.run(_run_download(*args, **kwargs))


def _print_history(settings: Settings) -> None:
    database = DownloadDatabase(settings)
    try:
        records = database.recent(20)
        stats = database.stats()
    finally:
        database.close()

    if not records:
        console.print("[yellow]数据库中没有下载记录[/yellow]")
        return

    table = Table(title="最近下载记录")
    for column in ("平台", "标题", "画质", "状态", "文件"):
        table.add_column(column, overflow="fold")
    for record in records:
        table.add_row(
            record.platform.value,
            record.title[:60],
            record.quality_label or "-",
            record.status.value,
            record.file_path or "-",
        )
    console.print(table)
    console.print("状态统计：" + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))


def _print_platforms(settings: Settings) -> None:
    table = Table(title="已注册平台")
    for column in ("平台", "标识", "下载通道", "需要 --confirm-rights"):
        table.add_column(column)
    client = build_client(settings)
    registry = None
    try:
        registry = build_registry(settings, client)
        for adapter in registry.adapters:
            official = not adapter.requires_rights_confirmation
            table.add_row(
                adapter.platform.display_name,
                adapter.platform.value,
                "官方 API" if official else "官方元数据 + 公开接口下载",
                "否" if official else "是",
            )
    finally:
        if registry is not None:
            for extra in registry.owned_clients:
                asyncio.run(extra.aclose())
        asyncio.run(client.aclose())
    console.print(table)


def _environment_report(settings: Settings) -> None:
    ffmpeg = locate_ffmpeg(settings)
    proxy = settings.proxy or "直连"
    bypass = ", ".join(sorted(settings.proxy_bypass_set)) or "无"
    console.print(
        f"Python {sys.version.split()[0]} | ffmpeg {'已就绪' if ffmpeg else '缺失'} "
        f"| 代理 {proxy} | 绕过代理 {bypass} "
        f"| 输出目录 {settings.resolve_path(settings.output_dir)}"
    )


def main(
    url: str | None = typer.Argument(None, help="公开视频链接"),
    output: Path | None = typer.Option(None, "--output", "-o", help="保存目录"),
    quality: str = typer.Option("best", "--quality", "-q", help="画质：best/1080p/720p/audio 等"),
    force: bool = typer.Option(False, "--force", "-f", help="忽略数据库记录，强制重新下载"),
    confirm_rights: bool = typer.Option(
        False, "--confirm-rights", help="确认你拥有下载该内容的权限"
    ),
    history: bool = typer.Option(False, "--history", help="显示最近的下载记录"),
    list_platforms: bool = typer.Option(False, "--list-platforms", help="列出已注册平台"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出调试日志"),
    log_level: str | None = typer.Option(None, "--log-level", help="日志级别"),
) -> None:
    """下载公开视频。不带子命令，直接传入链接即可。"""

    settings = get_settings()
    if log_level:
        settings.log_level = log_level.upper()
    setup_logging(settings, console_level="DEBUG" if verbose else None)
    _environment_report(settings)

    if list_platforms:
        _print_platforms(settings)
        return
    if history:
        _print_history(settings)
        return
    if not url:
        console.print(
            "[yellow]请提供视频链接[/yellow]，例如：\n"
            f'  python main.py "https://www.bilibili.com/video/BV1xx411c7mD" '
            f"--output {PROJECT_ROOT / 'downloads'} --quality 1080p\n"
            "使用 --help 查看全部参数。"
        )
        raise typer.Exit(code=1)

    try:
        exit_code = _run_download_sync(
            settings,
            url,
            _validate_quality(quality),
            output,
            force,
            confirm_rights or settings.confirm_rights,
        )
    except VideoDownloaderError as exc:
        error_console.print(f"[red]错误：{exc}[/red]")
        if verbose and exc.detail:
            error_console.print(f"[dim]{exc.detail}[/dim]")
        raise typer.Exit(code=exc.exit_code) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive
        error_console.print("[yellow]已取消[/yellow]")
        raise typer.Exit(code=130) from None

    raise typer.Exit(code=exit_code)


if __name__ == "__main__":
    typer.run(main)
