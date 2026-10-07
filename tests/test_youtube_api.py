"""YouTube Data API helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from platforms.youtube.api import parse_duration, parse_timestamp, thumbnail_url


def test_parse_duration_hours_minutes_seconds() -> None:
    assert parse_duration("PT1H2M10S") == 3730.0


def test_parse_duration_minutes_only() -> None:
    assert parse_duration("PT3M33S") == 213.0


def test_parse_duration_days() -> None:
    assert parse_duration("P1DT1S") == 86401.0


def test_parse_duration_invalid() -> None:
    assert parse_duration(None) is None
    assert parse_duration("not-a-duration") is None


def test_parse_timestamp() -> None:
    assert parse_timestamp("2021-05-04T10:00:00Z") == datetime(2021, 5, 4, 10, 0, tzinfo=UTC)
    assert parse_timestamp(None) is None


def test_thumbnail_prefers_highest_resolution() -> None:
    item = {
        "snippet": {
            "thumbnails": {
                "default": {"url": "https://img/default.jpg"},
                "high": {"url": "https://img/high.jpg"},
            }
        }
    }
    assert thumbnail_url(item) == "https://img/high.jpg"
    assert thumbnail_url({}) is None
