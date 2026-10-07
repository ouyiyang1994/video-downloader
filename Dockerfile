# syntax=docker/dockerfile:1
# ============================================================================
#  Video Downloader — headless CLI image
# ----------------------------------------------------------------------------
#  This project is a desktop application (PySide6 GUI) *plus* a headless CLI
#  (main.py). A container has no display, so this image ships the **CLI** only:
#
#      docker run --rm -v "$PWD/downloads:/app/downloads" \
#          video-downloader "https://www.bilibili.com/video/BV1GJ411x7h7"
#
#  ffmpeg/ffprobe are installed from the distribution packages so that
#  DASH (separate video + audio) streams can be muxed.
# ============================================================================
FROM python:3.12-slim-bookworm

# --- system dependencies ----------------------------------------------------
#   ffmpeg  -> muxing DASH video+audio (required for HD downloads)
#   ca-certificates -> HTTPS to the platforms
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# --- uv (same resolver the project uses locally, honours uv.lock) -----------
RUN pip install --no-cache-dir uv

# Install the virtualenv outside /app so a bind-mounted source tree never hides it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1

WORKDIR /app

# --- dependency layer (cached until pyproject.toml / uv.lock change) --------
# PySide6/shiboken6 are GUI-only (imported by gui.py, never by main.py) and
# pull in hundreds of MB of Qt libraries, so they are excluded from this
# headless image. Everything else is installed straight from the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev \
        --no-install-package pyside6 \
        --no-install-package shiboken6

# --- application source -----------------------------------------------------
COPY config/    ./config/
COPY core/      ./core/
COPY platforms/ ./platforms/
COPY storage/   ./storage/
COPY main.py    ./main.py
COPY .env.example ./.env.example

# --- runtime data -----------------------------------------------------------
# The app resolves downloads/, logs/ and downloads.db against its own root
# (/app). Mount these to persist results and history outside the container.
RUN mkdir -p /app/downloads /app/logs
VOLUME ["/app/downloads", "/app/logs"]

# Optional: mount your own configuration read-only, e.g.
#   -v "$PWD/.env:/app/.env:ro"
# Without it the app runs on defaults (all credentials are optional).

ENTRYPOINT ["/opt/venv/bin/python", "main.py"]
CMD ["--help"]
