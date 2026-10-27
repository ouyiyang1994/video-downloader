"""Child process used by the cross-process output-claim tests.

Run as ``python tests/_mp_worker.py <mode> <directory> <stem> [suffix]``.
It prints the claimed path on the first line and then either waits for stdin to
close (``claim``), exits immediately (``claim-release``), dies without any
cleanup (``crash``) or performs a real download (``download``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.naming import _lock_file, claim_output_path, release_output_path  # noqa: E402


def _download(url: str, size: int, destination: Path) -> None:
    import httpx

    from config.settings import Settings
    from core.downloader import HttpDownloader
    from core.models import MediaStream

    async def run() -> None:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            downloader = HttpDownloader(Settings(_env_file=None), client)
            stream = MediaStream(
                url=url,
                quality_label="test",
                size=size,
                format_id="test",
            )
            await downloader.download_stream(stream, destination)

    asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["claim", "claim-release", "crash", "download"])
    parser.add_argument("directory")
    parser.add_argument("stem")
    parser.add_argument("suffix", nargs="?", default="mp4")
    parser.add_argument("--url", default="")
    parser.add_argument("--size", type=int, default=0)
    args = parser.parse_args()

    destination = claim_output_path(Path(args.directory), args.stem, args.suffix)
    print(destination, flush=True)
    print(f"lock={_lock_file(destination)}", file=sys.stderr, flush=True)

    if args.mode == "claim":
        # Hold the claim until the parent closes stdin.
        sys.stdin.readline()
        return 0
    if args.mode == "claim-release":
        release_output_path(destination)
        return 0
    if args.mode == "crash":
        # No release, no cleanup: the OS has to reclaim the lock.
        os._exit(9)
    _download(args.url, args.size, destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
