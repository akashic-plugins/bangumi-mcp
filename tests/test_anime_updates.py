from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.anilist import AniListNotFoundError
from src.anime_updates import (
    AnimeUpdateCoordinator,
    AnimeUpdateStateError,
    AnimeUpdateStore,
    CatalogEpisode,
    CatalogSubject,
)
from src.client import BangumiApiError
from src.config import AnimePushRuntimeConfig


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


class FakeBangumi:
    def __init__(self, *, episode_type: int = 0) -> None:
        self.episode_type = episode_type
        self.episode_collection_calls: list[int] = []

    def get_me(self) -> dict[str, Any]:
        return {"username": "lf_egg"}

    def list_collections(self, username: str, **kwargs: Any) -> dict[str, Any]:
        assert username == "lf_egg"
        assert kwargs == {
            "subject_type": 2,
            "collection_type": 3,
            "limit": 50,
            "offset": 0,
        }
        return {
            "total": 1,
            "limit": 50,
            "offset": 0,
            "data": [{"subject_id": 42}],
        }

    def get_subject(self, subject_id: int) -> dict[str, Any]:
        assert subject_id == 42
        return {
            "id": 42,
            "name": "Test Anime",
            "name_cn": "测试动画",
            "date": "2026-07-01",
            "platform": "TV",
            "eps": 8,
        }

    def list_episodes(
        self,
        subject_id: int,
        *,
        episode_type: int = 0,
    ) -> list[dict[str, Any]]:
        assert subject_id == 42
        assert episode_type == 0
        return [
            {
                "id": 1000 + number,
                "type": 0,
                "sort": number,
                "name": f"Episode {number}",
                "name_cn": "",
                "airdate": f"2026-08-{number:02d}",
            }
            for number in range(1, 9)
        ]

    def get_episode_collection(self, episode_id: int) -> dict[str, Any]:
        self.episode_collection_calls.append(episode_id)
        return {
            "episode": {"id": episode_id},
            "type": self.episode_type,
            "updated_at": 0,
        }


class FailingBangumi(FakeBangumi):
    def get_episode_collection(self, episode_id: int) -> dict[str, Any]:
        self.episode_collection_calls.append(episode_id)
        raise BangumiApiError("temporary", status_code=500)


class PostponingBangumi(FakeBangumi):
    def __init__(self, store: AnimeUpdateStore) -> None:
        super().__init__()
        self.store = store

    def get_episode_collection(self, episode_id: int) -> dict[str, Any]:
        self.store.upsert_schedule(
            episode_id=episode_id,
            subject_id=42,
            media_id=154587,
            episode_number=7,
            airing_at=int(NOW.timestamp()) + 3600,
            notify_at=int(NOW.timestamp()) + 3600,
            mapping_fingerprint="mapping-fingerprint",
            now=NOW + timedelta(seconds=1),
        )
        return super().get_episode_collection(episode_id)


class FakeAniList:
    def __init__(self, next_episode: int = 7) -> None:
        self.next_episode = next_episode
        self.search_calls: list[str] = []
        self.get_calls: list[int] = []

    def media(self) -> dict[str, Any]:
        return {
            "id": 154587,
            "type": "ANIME",
            "format": "TV",
            "status": "RELEASING",
            "episodes": 8,
            "season": "SUMMER",
            "seasonYear": 2026,
            "startDate": {"year": 2026, "month": 7, "day": 1},
            "title": {
                "romaji": "Test Anime",
                "english": "Test Anime",
                "native": "Test Anime",
            },
            "nextAiringEpisode": {
                "episode": self.next_episode,
                "airingAt": int(NOW.timestamp()) + self.next_episode * 3600,
            },
        }

    def search_anime(self, search: str) -> list[dict[str, Any]]:
        self.search_calls.append(search)
        return [self.media()]

    def get_anime(self, media_id: int) -> dict[str, Any]:
        self.get_calls.append(media_id)
        assert media_id == 154587
        return self.media()


class MissingAniList(FakeAniList):
    def get_anime(self, media_id: int) -> dict[str, Any]:
        raise AniListNotFoundError("missing")


def store_at(tmp_path: Path) -> AnimeUpdateStore:
    store = AnimeUpdateStore(tmp_path / "anime_updates.db")
    store.initialize()
    return store


def seed_schedule(
    store: AnimeUpdateStore,
    *,
    episode_id: int = 1007,
    notify_at: int | None = None,
) -> None:
    subject = CatalogSubject(
        subject_id=42,
        title="测试动画",
        original_title="Test Anime",
        air_date="2026-07-01",
        subject_format="TV",
        episode_count=8,
        fingerprint="subject-fingerprint",
    )
    episode_number = episode_id - 1000
    store.upsert_catalog(
        subject,
        [
            CatalogEpisode(
                episode_id=episode_id,
                subject_id=42,
                episode_number=episode_number,
                title=f"Episode {episode_number}",
                air_date="2026-08-05",
            )
        ],
        NOW,
    )
    store.save_mapping(
        subject_id=42,
        media_id=154587,
        method="override",
        subject_fingerprint="subject-fingerprint",
        fingerprint="mapping-fingerprint",
        now=NOW,
    )
    store.upsert_schedule(
        episode_id=episode_id,
        subject_id=42,
        media_id=154587,
        episode_number=episode_number,
        airing_at=int(NOW.timestamp()),
        notify_at=int(NOW.timestamp()) if notify_at is None else notify_at,
        mapping_fingerprint="mapping-fingerprint",
        now=NOW,
    )


def coordinator(
    store: AnimeUpdateStore,
    bangumi: FakeBangumi,
    *,
    anilist: FakeAniList | None = None,
    config: AnimePushRuntimeConfig | None = None,
    now: datetime = NOW,
) -> AnimeUpdateCoordinator:
    return AnimeUpdateCoordinator(
        store,
        bangumi,
        anilist or FakeAniList(),
        config or AnimePushRuntimeConfig(enabled=True),
        clock=lambda: now,
        jitter=lambda seconds: seconds,
    )


def database_row(store: AnimeUpdateStore, query: str) -> sqlite3.Row:
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(query).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row


def test_due_unwatched_episode_creates_one_stable_pending_event(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)
    bangumi = FakeBangumi(episode_type=0)
    updates = coordinator(store, bangumi)

    updates.evaluate_due()
    updates.evaluate_due()

    events = store.fetch_pending(0, 50)
    assert len(events) == 1
    assert events[0]["event_id"] == "episode:1007"
    assert events[0]["kind"] == "alert"
    assert events[0]["title"] == "《测试动画》第 7 集计划于 20:00 放送"
    assert "不代表任何平台已经上线" in str(events[0]["content"])
    assert bangumi.episode_collection_calls == [1007]


def test_watched_episode_is_suppressed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)

    coordinator(store, FakeBangumi(episode_type=2)).evaluate_due()

    assert store.fetch_pending(0, 50) == []
    assert database_row(store, "SELECT state FROM schedules")["state"] == "suppressed"


def test_due_failure_retries_without_assuming_unwatched(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)

    coordinator(store, FailingBangumi()).evaluate_due()

    assert store.fetch_pending(0, 50) == []
    row = database_row(
        store,
        "SELECT state, failure_count, next_retry_at FROM schedules",
    )
    assert row["state"] == "scheduled"
    assert row["failure_count"] == 1
    assert row["next_retry_at"] == int(NOW.timestamp()) + 900


def test_unchanged_schedule_refresh_preserves_due_retry_state(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)
    coordinator(store, FailingBangumi()).evaluate_due()

    store.upsert_schedule(
        episode_id=1007,
        subject_id=42,
        media_id=154587,
        episode_number=7,
        airing_at=int(NOW.timestamp()),
        notify_at=int(NOW.timestamp()),
        mapping_fingerprint="mapping-fingerprint",
        now=NOW + timedelta(minutes=1),
    )

    row = database_row(
        store,
        "SELECT failure_count, next_retry_at, last_error FROM schedules",
    )
    assert row["failure_count"] == 1
    assert row["next_retry_at"] == int(NOW.timestamp()) + 900
    assert row["last_error"] == "http_500"


def test_changed_schedule_clears_old_due_retry_state(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)
    coordinator(store, FailingBangumi()).evaluate_due()

    store.upsert_schedule(
        episode_id=1007,
        subject_id=42,
        media_id=154587,
        episode_number=7,
        airing_at=int(NOW.timestamp()) + 3600,
        notify_at=int(NOW.timestamp()) + 3600,
        mapping_fingerprint="mapping-fingerprint",
        now=NOW + timedelta(minutes=1),
    )

    row = database_row(
        store,
        "SELECT failure_count, next_retry_at, last_error FROM schedules",
    )
    assert row["failure_count"] == 0
    assert row["next_retry_at"] == 0
    assert row["last_error"] is None


def test_missed_schedule_expires_without_remote_lookup(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(
        store,
        notify_at=int(NOW.timestamp()) - 6 * 60 * 60 - 1,
    )
    bangumi = FakeBangumi()

    coordinator(store, bangumi).evaluate_due()

    assert bangumi.episode_collection_calls == []
    assert database_row(store, "SELECT state FROM schedules")["state"] == "expired"


def test_stale_ineligibility_snapshot_cannot_expire_current_valid_schedule(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)

    store.expire_schedule(
        1007,
        NOW,
        expected_notify_at=int(NOW.timestamp()),
        expected_mapping_fingerprint="mapping-fingerprint",
    )

    assert database_row(store, "SELECT state FROM schedules")["state"] == "scheduled"


def test_concurrent_postponement_prevents_stale_pending_event(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)

    coordinator(store, PostponingBangumi(store)).evaluate_due()

    assert store.fetch_pending(0, 50) == []
    row = database_row(store, "SELECT state, notify_at FROM schedules")
    assert row["state"] == "scheduled"
    assert row["notify_at"] == int(NOW.timestamp()) + 3600


def test_ack_is_idempotent_and_unknown_batch_rolls_back(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)
    coordinator(store, FakeBangumi()).evaluate_due()

    with pytest.raises(AnimeUpdateStateError, match="未知"):
        store.acknowledge(["episode:1007", "episode:9999"], NOW)
    assert len(store.fetch_pending(0, 50)) == 1

    assert store.acknowledge(["episode:1007"], NOW) == 1
    assert store.fetch_pending(0, 50) == []
    assert store.acknowledge(["episode:1007"], NOW) == 0
    assert database_row(store, "SELECT state FROM schedules")["state"] == "acked"


def test_ack_rejects_cross_table_state_mismatch(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)
    coordinator(store, FakeBangumi()).evaluate_due()
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE schedules SET state = 'acked'")
    connection.commit()
    connection.close()

    with pytest.raises(AnimeUpdateStateError, match="不可确认"):
        store.acknowledge(["episode:1007"], NOW)

    assert len(store.fetch_pending(0, 50)) == 1


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_fetch_rejects_invalid_pagination(tmp_path: Path, value: object) -> None:
    store = store_at(tmp_path)

    with pytest.raises(ValueError):
        store.fetch_pending(value, 50)  # type: ignore[arg-type]


def test_catalog_and_schedule_refresh_cache_successive_future_episodes(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    bangumi = FakeBangumi()
    anilist = FakeAniList(next_episode=7)
    config = AnimePushRuntimeConfig(
        enabled=True,
        media_id_overrides={42: 154587},
    )
    updates = coordinator(store, bangumi, anilist=anilist, config=config)

    updates.refresh_catalog()
    updates.refresh_schedules()
    anilist.next_episode = 8
    updates.refresh_schedules()

    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute(
            "SELECT episode_id, state FROM schedules ORDER BY episode_id"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [(1007, "scheduled"), (1008, "scheduled")]
    assert anilist.get_calls == [154587, 154587]


def test_automatic_mapping_requires_unique_strict_candidate(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    bangumi = FakeBangumi()
    anilist = FakeAniList()
    updates = coordinator(store, bangumi, anilist=anilist)
    updates.refresh_catalog()

    updates.refresh_schedules()

    mapping = store.mapping_for(42)
    assert mapping is not None
    assert mapping["valid"] == 1
    assert mapping["method"] == "automatic"
    assert anilist.search_calls == ["Test Anime"]


def test_missing_explicit_override_invalidates_old_mapping(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    seed_schedule(store)
    config = AnimePushRuntimeConfig(
        enabled=True,
        media_id_overrides={42: 999999},
    )

    coordinator(
        store,
        FakeBangumi(),
        anilist=MissingAniList(),
        config=config,
    ).refresh_schedules()

    mapping = store.mapping_for(42)
    assert mapping is not None
    assert mapping["valid"] == 0
    assert mapping["error_code"] == "override_not_found"


def test_unknown_or_incomplete_schema_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "anime_updates.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(AnimeUpdateStateError, match="schema"):
        AnimeUpdateStore(path).initialize()


def test_refresh_poll_uses_persisted_exponential_backoff(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    updates = coordinator(store, FakeBangumi())

    def fail() -> None:
        raise BangumiApiError("temporary", status_code=500)

    first = asyncio.run(updates._run_step("catalog", fail, 3600))
    second = asyncio.run(updates._run_step("catalog", fail, 3600))

    assert first == 900
    assert second == 1800
    assert store.poll_failure_count("catalog") == 2
    assert store.poll_next_run_at("catalog") == int(NOW.timestamp()) + 1800


def test_coordinator_restart_respects_persisted_next_run(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    future = int(NOW.timestamp()) + 3600
    for kind in ("catalog", "schedule", "due"):
        store.record_poll(
            kind,
            success=True,
            next_run_at=future,
            error_code=None,
            now=NOW,
        )
    updates = coordinator(store, FakeBangumi())
    calls: list[str] = []
    updates.refresh_catalog = lambda: calls.append("catalog")  # type: ignore[method-assign]
    updates.refresh_schedules = lambda: calls.append("schedule")  # type: ignore[method-assign]
    updates.evaluate_due = lambda: calls.append("due")  # type: ignore[method-assign]

    async def exercise() -> None:
        task = asyncio.create_task(updates.run())
        await asyncio.sleep(0)
        assert calls == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_due_interval_is_not_jittered(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    updates = AnimeUpdateCoordinator(
        store,
        FakeBangumi(),
        FakeAniList(),
        AnimePushRuntimeConfig(enabled=True),
        clock=lambda: NOW,
        jitter=lambda seconds: seconds * 2,
    )

    due_delay = asyncio.run(updates._run_step("due", lambda: None, 60))
    catalog_delay = asyncio.run(
        updates._run_step("catalog", lambda: None, 3600)
    )

    assert due_delay == 60
    assert catalog_delay == 7200
