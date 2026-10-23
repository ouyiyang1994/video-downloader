"""Persistence for the download queue.

The queue's whole promise is that pending work survives a restart, so these
tests are about what comes back out of SQLite, not about scheduling.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.settings import Settings
from core.queue import QueueState
from core.queue_store import (
    FINISHED_STATES,
    UNFINISHED_STATES,
    QueueRow,
    QueueStore,
)


def _row(task_id: str, url: str, **overrides: object) -> QueueRow:
    defaults: dict[str, object] = {
        "task_id": task_id,
        "url": url,
        "quality": "best",
        "state": "pending",
        "attempts": 0,
        "max_attempts": 2,
        "position": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return QueueRow(**defaults)  # type: ignore[arg-type]


def test_a_row_round_trips(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(
            _row(
                "abc",
                "https://example.test/v/1",
                title="示例",
                video_id="BV1fkHD6wEXt_p1",
                state="running",
                attempts=1,
                output_dir="D:/videos",
                error="上次失败",
            )
        )
        rows = store.load_all()
    finally:
        store.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == "abc"
    assert row.title == "示例"
    assert row.video_id == "BV1fkHD6wEXt_p1"
    assert row.state == "running"
    assert row.attempts == 1
    assert row.output_dir == "D:/videos"
    assert row.error == "上次失败"


def test_upsert_updates_rather_than_duplicates(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("abc", "https://example.test/v/1"))
        store.upsert(_row("abc", "https://example.test/v/1", state="completed", attempts=2))
        rows = store.load_all()
    finally:
        store.close()

    assert len(rows) == 1
    assert rows[0].state == "completed"
    assert rows[0].attempts == 2


def test_rows_come_back_in_queue_order(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("third", "u3", position=3))
        store.upsert(_row("first", "u1", position=1))
        store.upsert(_row("second", "u2", position=2))
        order = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert order == ["first", "second", "third"]


def test_only_unfinished_rows_are_restored(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("a", "u1", position=1, state="pending"))
        store.upsert(_row("b", "u2", position=2, state="running"))
        store.upsert(_row("c", "u3", position=3, state="paused"))
        store.upsert(_row("d", "u4", position=4, state="completed"))
        store.upsert(_row("e", "u5", position=5, state="failed"))
        unfinished = [row.task_id for row in store.load_unfinished()]
    finally:
        store.close()

    assert unfinished == ["a", "b", "c"]


def test_a_url_waiting_in_the_queue_is_detected(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("a", "https://example.test/v/1", state="pending"))
        store.upsert(_row("b", "https://example.test/v/2", state="completed"))

        assert store.has_unfinished_url("https://example.test/v/1") is True
        # A finished row must not block a fresh submission of the same link.
        assert store.has_unfinished_url("https://example.test/v/2") is False
        assert store.has_unfinished_url("https://example.test/v/3") is False
    finally:
        store.close()


def test_has_unfinished_url_can_exclude_one_task(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("a", "https://example.test/v/1", state="pending"))
        assert store.has_unfinished_url("https://example.test/v/1", exclude="a") is False
    finally:
        store.close()


def test_next_position_follows_the_highest(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        assert store.next_position() == 1
        store.upsert(_row("a", "u1", position=7))
        assert store.next_position() == 8
    finally:
        store.close()


def test_delete_finished_keeps_the_pending_work(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("a", "u1", position=1, state="pending"))
        store.upsert(_row("b", "u2", position=2, state="completed"))
        store.upsert(_row("c", "u3", position=3, state="failed"))
        removed = store.delete_finished()
        remaining = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert removed == 2
    assert remaining == ["a"]


def test_delete_finished_keeps_a_row_it_cannot_read(settings: Settings) -> None:
    """Clearing the list must not reach rows that were never in the list."""

    store = QueueStore(settings)
    try:
        store.upsert(_row("future", "u1", position=1, state="verifying"))
        store.upsert(_row("done", "u2", position=2, state="completed"))
        removed = store.delete_finished()
        remaining = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert removed == 1
    assert remaining == ["future"]


def test_prune_forgets_old_finished_rows_only(settings: Settings) -> None:
    store = QueueStore(settings)
    old = datetime.now(UTC) - timedelta(days=30)
    try:
        store.upsert(_row("old", "u1", position=1, state="completed", updated_at=old))
        store.upsert(_row("new", "u2", position=2, state="completed"))
        store.upsert(_row("pending", "u3", position=3, state="pending", updated_at=old))
        removed = store.prune(days=7)
        remaining = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert removed == 1
    assert sorted(remaining) == ["new", "pending"]


@pytest.mark.parametrize("state", ["verifying", "", "PENDING"])
def test_prune_keeps_a_row_whose_state_it_cannot_read(
    settings: Settings, state: str
) -> None:
    """A state this build does not know must never be treated as "finished".

    ``prune`` used to delete everything outside ``UNFINISHED_STATES``, so a row
    written by a newer version (or edited by hand) was silently dropped after
    the retention window - exactly the work the user would want to keep.
    """

    store = QueueStore(settings)
    old = datetime.now(UTC) - timedelta(days=30)
    try:
        store.upsert(_row("future", "u1", position=1, state=state, updated_at=old))
        removed = store.prune(days=7)
        remaining = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert removed == 0, "无法解析的状态不能被自动清理"
    assert remaining == ["future"]


@pytest.mark.parametrize("state", FINISHED_STATES)
def test_prune_drops_old_rows_in_every_terminal_state(
    settings: Settings, state: str
) -> None:
    store = QueueStore(settings)
    old = datetime.now(UTC) - timedelta(days=30)
    try:
        store.upsert(_row("done", "u1", position=1, state=state, updated_at=old))
        removed = store.prune(days=7)
        remaining = store.load_all()
    finally:
        store.close()

    assert removed == 1, f"终态 {state!r} 的旧记录应当被清理"
    assert remaining == []


@pytest.mark.parametrize("state", UNFINISHED_STATES)
def test_prune_never_touches_unfinished_rows(settings: Settings, state: str) -> None:
    store = QueueStore(settings)
    old = datetime.now(UTC) - timedelta(days=365)
    try:
        store.upsert(_row("waiting", "u1", position=1, state=state, updated_at=old))
        removed = store.prune(days=7)
        remaining = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert removed == 0
    assert remaining == ["waiting"]


def test_prune_stops_at_the_retention_boundary(settings: Settings) -> None:
    """One hour inside the window is kept, one hour outside is dropped."""

    now = datetime.now(UTC)
    inside = now - timedelta(days=7) + timedelta(hours=1)
    outside = now - timedelta(days=7) - timedelta(hours=1)

    store = QueueStore(settings)
    try:
        store.upsert(_row("inside", "u1", position=1, state="completed", updated_at=inside))
        store.upsert(_row("outside", "u2", position=2, state="completed", updated_at=outside))
        removed = store.prune(days=7)
        remaining = [row.task_id for row in store.load_all()]
    finally:
        store.close()

    assert removed == 1
    assert remaining == ["inside"]


def test_the_state_lists_cover_the_enum_exactly() -> None:
    """The two literals here mirror ``QueueState``; drift would be silent."""

    every = {state.value for state in QueueState}
    finished = {state.value for state in QueueState if state.finished}

    assert set(FINISHED_STATES) == finished, "FINISHED_STATES 与 QueueState.finished 不一致"
    assert set(UNFINISHED_STATES) == every - finished
    assert not set(FINISHED_STATES) & set(UNFINISHED_STATES)


def test_delete_removes_a_row(settings: Settings) -> None:
    store = QueueStore(settings)
    try:
        store.upsert(_row("a", "u1"))
        store.delete("a")
        assert store.load_all() == []
    finally:
        store.close()


def test_a_table_from_an_earlier_build_gains_the_new_columns(settings: Settings) -> None:
    """``CREATE TABLE IF NOT EXISTS`` cannot add a column, so we migrate."""

    path: Path = settings.resolve_path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE queue_items (
            task_id      TEXT PRIMARY KEY,
            url          TEXT NOT NULL,
            quality      TEXT NOT NULL DEFAULT 'best',
            title        TEXT,
            state        TEXT NOT NULL DEFAULT 'pending',
            attempts     INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            position     INTEGER NOT NULL DEFAULT 0,
            error        TEXT,
            file_path    TEXT,
            file_size    INTEGER,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO queue_items (task_id, url, state, created_at, updated_at) "
        "VALUES ('legacy', 'https://example.test/old', 'pending', '2020-01-01T00:00:00+00:00', "
        "'2020-01-01T00:00:00+00:00')"
    )
    legacy.commit()
    legacy.close()

    store = QueueStore(settings)
    try:
        rows = store.load_all()
        # Writing proves the columns exist; reading proves old rows survive.
        store.upsert(
            _row("fresh", "u2", position=2, output_dir="D:/out", video_id="BV1_p1")
        )
        after = {row.task_id: row for row in store.load_all()}
    finally:
        store.close()

    assert [row.task_id for row in rows] == ["legacy"]
    # The columns a pre-1.27 build never wrote come back empty, not missing.
    assert rows[0].output_dir is None
    assert rows[0].video_id is None
    assert after["fresh"].video_id == "BV1_p1"
