"""Build the application's Windows ``.ico`` from the master PNG.

Scaling is done with the ffmpeg binary the project already ships, and the
frames are packed into a Vista-style PNG-compressed ``.ico`` - the format
Windows, PyInstaller and Inno Setup all accept. No extra dependency is needed.

Usage::

    python packaging/make_icon.py --source path/to/master.png
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from core.merger import locate_ffmpeg  # noqa: E402

#: Every size an ICO should carry for Windows shell + Explorer + shortcuts.
ICON_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 256)

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "VideoDownloader.ico"


def render_frame(ffmpeg: Path, source: Path, size: int, destination: Path) -> bytes:
    """Scale ``source`` to ``size``x``size`` (alpha preserved) and return the PNG."""

    command = [
        str(ffmpeg),
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale={size}:{size}:flags=lanczos",
        "-pix_fmt",
        "rgba",
        str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return destination.read_bytes()


def build_ico(frames: list[tuple[int, bytes]]) -> bytes:
    """Pack PNG frames into an ICO container."""

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries = bytearray()
    payload = bytearray()
    for size, data in frames:
        # 256 is encoded as 0 in the directory entry.
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return bytes(header + entries + payload)


def read_ico_sizes(path: Path) -> list[int]:
    """Read the frame sizes back out of an ICO (used by the self-check)."""

    data = path.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or image_type != 1:
        raise ValueError("not an ICO file")
    sizes = []
    for index in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + 16 * index)
        sizes.append(width or 256)
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成应用程序 .ico")
    parser.add_argument("--source", required=True, type=Path, help="母版 PNG（正方形，带透明圆角）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path, help="输出 .ico 路径")
    parser.add_argument("--keep-png", type=Path, help="可选：保留 256×256 预览 PNG 到该目录")
    args = parser.parse_args(argv)

    source: Path = args.source.resolve()
    if not source.is_file():
        print(f"[错误] 找不到母版图像: {source}", file=sys.stderr)
        return 2

    ffmpeg = locate_ffmpeg(get_settings())
    if ffmpeg is None:
        print("[错误] 找不到 ffmpeg（tools/ffmpeg/bin 或 PATH）", file=sys.stderr)
        return 2

    output: Path = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vd-icon-") as workspace:
        frames: list[tuple[int, bytes]] = []
        for size in ICON_SIZES:
            target = Path(workspace) / f"{size}.png"
            data = render_frame(ffmpeg, source, size, target)
            frames.append((size, data))
            print(f"  {size:>3}x{size:<3} {len(data):>7d} 字节")
        output.write_bytes(build_ico(frames))

        if args.keep_png is not None:
            args.keep_png.mkdir(parents=True, exist_ok=True)
            (args.keep_png / "icon-preview-256.png").write_bytes(frames[-1][1])

    sizes = read_ico_sizes(output)
    print(f"\n已生成 {output}")
    print(f"  大小 {output.stat().st_size} 字节，包含帧: {sizes}")
    missing = [size for size in ICON_SIZES if size not in sizes]
    if missing:
        print(f"[错误] 缺少尺寸: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
