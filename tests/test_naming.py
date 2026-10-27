"""Filename sanitisation and directory templating."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import naming
from core.exceptions import OutputLockError, OutputNameConflictError, PathTooLongError
from core.models import Platform
from core.naming import (
    LEAF_RESERVE,
    MAX_FULL_PATH,
    MIN_STEM,
    build_output_directory,
    claim_output_path,
    release_output_path,
    sanitize_filename,
    unique_path,
)


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


def test_claim_output_path_uses_the_plain_name_when_free(tmp_path: Path) -> None:
    claimed = claim_output_path(tmp_path, "clip", "mp4")
    try:
        assert claimed == tmp_path / "clip.mp4"
        # Claiming reserves the name without touching the filesystem.
        assert not claimed.exists()
    finally:
        release_output_path(claimed)


def test_claim_output_path_skips_a_file_that_is_already_there(tmp_path: Path) -> None:
    (tmp_path / "clip.mp4").write_bytes(b"x")
    claimed = claim_output_path(tmp_path, "clip", "mp4")
    try:
        assert claimed == tmp_path / "clip (1).mp4"
    finally:
        release_output_path(claimed)


def test_claim_output_path_holds_the_name_until_released(tmp_path: Path) -> None:
    """Two workers must not be handed the same name before either writes."""

    first = claim_output_path(tmp_path, "clip", "mp4")
    second = claim_output_path(tmp_path, "clip", "mp4")
    try:
        assert first == tmp_path / "clip.mp4"
        assert second == tmp_path / "clip (1).mp4"
    finally:
        release_output_path(first)
        release_output_path(second)

    third = claim_output_path(tmp_path, "clip", "mp4")
    try:
        assert third == tmp_path / "clip.mp4"
    finally:
        release_output_path(third)


def test_claim_output_path_reuses_the_file_of_the_same_video(tmp_path: Path) -> None:
    """A forced re-download keeps its own name instead of piling up copies."""

    existing = tmp_path / "clip.mp4"
    existing.write_bytes(b"old bytes")
    claimed = claim_output_path(tmp_path, "clip", "mp4", keep=existing)
    try:
        assert claimed == existing
    finally:
        release_output_path(claimed)


def test_claim_output_path_keep_does_not_cover_another_videos_file(tmp_path: Path) -> None:
    (tmp_path / "clip.mp4").write_bytes(b"someone else's video")
    keep = tmp_path / "clip.mkv"
    claimed = claim_output_path(tmp_path, "clip", "mp4", keep=keep)
    try:
        assert claimed == tmp_path / "clip (1).mp4"
    finally:
        release_output_path(claimed)


def test_claim_output_path_gives_up_after_the_limit(tmp_path: Path) -> None:
    (tmp_path / "clip.mp4").write_bytes(b"x")
    with pytest.raises(OutputNameConflictError) as excinfo:
        claim_output_path(tmp_path, "clip", "mp4", limit=1)

    # The queue shows ``message``, so it has to explain the situation itself.
    assert "同名" in excinfo.value.message
    assert excinfo.value.detail is not None


# --------------------------------------------------------------------------
# Whole-path budget (Windows MAX_PATH)
# --------------------------------------------------------------------------


def _deep_base(tmp_path: Path, *, target: int = 150) -> Path:
    """Create a real nested folder whose absolute path is at least ``target``."""

    base = tmp_path
    index = 0
    while len(str(base)) < target:
        base = base / (chr(ord("d") + index % 20) * 40)
        base.mkdir()
        index += 1
    return base


def _long_directory(tmp_path: Path, *, target: int = 150) -> Path:
    base = _deep_base(tmp_path, target=target)
    directory = build_output_directory(
        base,
        platform=Platform.BILIBILI,
        author="某UP主",
        publish_time=datetime(2026, 10, 25, tzinfo=UTC),
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_a_long_title_is_shortened_so_the_whole_path_fits(tmp_path: Path) -> None:
    directory = _long_directory(tmp_path)
    claimed = claim_output_path(directory, "标" * 120, "mp4")
    try:
        assert len(str(claimed)) + LEAF_RESERVE <= MAX_FULL_PATH
        assert len(claimed.stem) < 120
        claimed.write_bytes(b"x")
        assert claimed.exists()
    finally:
        release_output_path(claimed)


def test_the_temporary_part_file_still_fits_the_budget(tmp_path: Path) -> None:
    directory = _long_directory(tmp_path)
    claimed = claim_output_path(directory, "标" * 120, "mp4")
    part = claimed.with_name(claimed.name + ".part")
    try:
        assert len(str(part)) <= MAX_FULL_PATH
        part.write_bytes(b"partial")
        assert part.exists()
    finally:
        part.unlink(missing_ok=True)
        release_output_path(claimed)


def test_merged_stream_names_still_fit_the_budget(tmp_path: Path) -> None:
    """``<stem>.video.webm.part`` is the longest leaf the pipeline builds."""

    directory = _long_directory(tmp_path)
    claimed = claim_output_path(directory, "标" * 120, "mp4")
    stream = claimed.with_name(f"{claimed.stem}.video.webm")
    try:
        assert len(str(stream) + ".part") <= MAX_FULL_PATH
        assert len(str(claimed.with_suffix(".mp4.part"))) <= MAX_FULL_PATH
    finally:
        release_output_path(claimed)


def test_duplicate_numbering_stays_within_the_budget(tmp_path: Path) -> None:
    directory = _long_directory(tmp_path)
    first = claim_output_path(directory, "标" * 120, "mp4")
    first.write_bytes(b"first")
    release_output_path(first)

    second = claim_output_path(directory, "标" * 120, "mp4")
    try:
        assert second != first
        assert "(1)" in second.name
        assert len(str(second)) + LEAF_RESERVE <= MAX_FULL_PATH
        second.write_bytes(b"second")
        assert first.read_bytes() == b"first"
    finally:
        release_output_path(second)


@pytest.mark.parametrize("suffix", ["mp4", "webm", "m4a"])
def test_every_extension_fits_the_budget(tmp_path: Path, suffix: str) -> None:
    directory = _long_directory(tmp_path)
    claimed = claim_output_path(directory, "标" * 120, suffix)
    try:
        assert claimed.suffix == f".{suffix}"
        assert len(str(claimed)) + LEAF_RESERVE <= MAX_FULL_PATH
    finally:
        release_output_path(claimed)


def test_an_oversized_base_folder_is_rejected_and_nothing_is_created(tmp_path: Path) -> None:
    too_long = tmp_path / ("z" * 200)
    with pytest.raises(PathTooLongError):
        build_output_directory(
            too_long,
            platform=Platform.BILIBILI,
            author="某UP主",
            publish_time=datetime(2026, 10, 25, tzinfo=UTC),
        )
    assert not too_long.exists()


def test_the_author_is_shortened_before_the_path_is_rejected(tmp_path: Path) -> None:
    base = _deep_base(tmp_path, target=150)
    directory = build_output_directory(
        base,
        platform=Platform.BILIBILI,
        author="作" * 60,
        publish_time=datetime(2026, 10, 25, tzinfo=UTC),
    )
    assert len(str(directory)) <= MAX_FULL_PATH - 1 - MIN_STEM - LEAF_RESERVE
    # Platform and date are structural: they must survive untouched.
    assert directory.parts[-3] == "bilibili"
    assert directory.parts[-1] == "2026-10-25"
    assert len(directory.parts[-2]) < 60


def test_claim_refuses_a_directory_that_leaves_no_room(tmp_path: Path) -> None:
    long_directory = tmp_path / ("y" * 200)
    with pytest.raises(PathTooLongError):
        claim_output_path(long_directory, "title", "mp4")


def test_a_name_that_cannot_be_locked_is_treated_as_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never claim a name whose lock could not be established.

    Two instances taking the same name is exactly what the lock exists to
    prevent, so an unusable lock file must push the claim to the next name
    instead of falling back to in-process-only claiming.
    """

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(lock_dir))
    naming._lock_file(tmp_path / "clip.mp4").mkdir()  # a directory cannot be a lock file

    claimed = claim_output_path(tmp_path, "clip", "mp4")
    try:
        assert claimed == tmp_path / "clip (1).mp4"
    finally:
        release_output_path(claimed)


def test_an_unusable_lock_directory_stops_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the cross-process lock there is no safe way to write at all.

    Degrading to an in-process-only claim is exactly what let two instances
    write the same file, so this must fail loudly instead.
    """

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(blocker))

    with pytest.raises(OutputLockError) as excinfo:
        claim_output_path(tmp_path, "clip", "mp4")

    assert "锁" in excinfo.value.message
    assert str(blocker) in excinfo.value.message
    assert not (tmp_path / "clip.mp4").exists()
    assert not (tmp_path / "clip.mp4.part").exists()
