"""Filename sanitisation and directory templating."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from core.models import Platform
from core.naming import build_output_directory, sanitize_filename, unique_path


def test_illegal_characters_are_replaced() -> None:
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_control_characters_and_whitespace() -> None:
    # Control characters are stripped, runs of whitespace collapse to one space,
    # but the space itself is preserved so titles stay readable.
    assert sanitize_filename("hello\x00\x1f   world") == "hello_ world"
    assert sanitize_filename("a\n\tb") == "a_b"


def test_reserved_windows_names() -> None:
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("nul.txt") == "_nul.txt"


def test_trailing_dots_and_spaces_removed() -> None:
    assert sanitize_filename("video...") == "video"


def test_empty_name_falls_back() -> None:
    assert sanitize_filename("   ") == "untitled"
    assert sanitize_filename("", fallback="clip") == "clip"


def test_length_is_capped() -> None:
    assert len(sanitize_filename("x" * 500, max_length=40)) == 40


def test_title_is_preserved_when_legal() -> None:
    title = "【官方 MV】Never Gonna Give You Up - Rick Astley"
    assert sanitize_filename(title) == title


def test_directory_template(tmp_path: Path) -> None:
    directory = build_output_directory(
        tmp_path,
        platform=Platform.BILIBILI,
        author="某UP主",
        publish_time=datetime(2021, 5, 4, tzinfo=UTC),
        template="{platform}/{author}/{date}",
    )
    assert directory == tmp_path / "bilibili" / "某UP主" / "2021-05-04"


def test_directory_template_falls_back_without_author(tmp_path: Path) -> None:
    directory = build_output_directory(
        tmp_path,
        platform=Platform.INSTAGRAM,
        author=None,
        publish_time=None,
        now=datetime(2024, 2, 29, tzinfo=UTC),
    )
    assert directory == tmp_path / "instagram" / "unknown_author" / "2024-02-29"


def test_unique_path(tmp_path: Path) -> None:
    existing = tmp_path / "clip.mp4"
    existing.write_bytes(b"x")
    assert unique_path(existing) == tmp_path / "clip (1).mp4"
