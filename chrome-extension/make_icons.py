"""Generate the extension's PNG icons without any third-party dependency.

Chrome's manifest cannot point at an SVG, and shipping four tiny PNGs is nicer
than showing the generic puzzle-piece icon. Run once; the files are committed.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BLUE = (31, 111, 235, 255)
WHITE = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """A blue rounded square with a white downward arrow."""

    radius = size * 0.22
    pixels = [[CLEAR] * size for _ in range(size)]

    def inside_round_rect(x: float, y: float) -> bool:
        cx = min(max(x, radius), size - radius)
        cy = min(max(y, radius), size - radius)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2

    # Arrow geometry, in fractions of the icon.
    shaft_half = size * 0.09
    shaft_top = size * 0.24
    shaft_bottom = size * 0.52
    head_half = size * 0.24
    head_top = size * 0.50
    tip_y = size * 0.76
    bar_half = size * 0.24
    bar_top = size * 0.83
    bar_bottom = size * 0.91

    for y in range(size):
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            if not inside_round_rect(px, py):
                continue
            colour = BLUE
            if abs(px - size / 2) <= shaft_half and shaft_top <= py <= shaft_bottom:
                colour = WHITE
            elif head_top <= py <= tip_y:
                half = head_half * (tip_y - py) / (tip_y - head_top)
                if abs(px - size / 2) <= half:
                    colour = WHITE
            elif abs(px - size / 2) <= bar_half and bar_top <= py <= bar_bottom:
                colour = WHITE
            pixels[y][x] = colour
    return pixels


def encode_png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    size = len(pixels)
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for pixel in row:
            raw.extend(pixel)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    target = Path(__file__).resolve().parent / "icons"
    target.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        path = target / f"icon{size}.png"
        path.write_bytes(encode_png(render(size)))
        print(f"wrote {path.relative_to(target.parent.parent)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
