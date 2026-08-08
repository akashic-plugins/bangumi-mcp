from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .anilist import AniListApiError, AniListNotFoundError
from .client import BangumiApiError
from .config import AnimePushRuntimeConfig


logger = logging.getLogger(__name__)
SCHEMA_VERSION = "1"
CATALOG_INTERVAL_SECONDS = 6 * 60 * 60
SCHEDULE_INTERVAL_SECONDS = 30 * 60
DUE_INTERVAL_SECONDS = 60
RECOVERY_WINDOW_SECONDS = 6 * 60 * 60
BASE_RETRY_SECONDS = 15 * 60
MAX_RETRY_SECONDS = 6 * 60 * 60


class AnimeUpdateStateError(RuntimeError):
    pass


class AnimeMappingError(RuntimeError):
    pass


class BangumiApi(Protocol):
    def get_me(self) -> dict[str, Any]: ...

    def get_subject(self, subject_id: int) -> dict[str, Any]: ...

    def list_collections(
        self,
        username: str,
        *,
        subject_type: int | None = None,
        collection_type: int | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def list_episodes(
        self,
        subject_id: int,
        *,
        episode_type: int = 0,
    ) -> list[dict[str, Any]]: ...

    def get_episode_collection(self, episode_id: int) -> dict[str, Any]: ...


class AniListApi(Protocol):
    def search_anime(self, search: str) -> list[dict[str, Any]]: ...

    def get_anime(self, media_id: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CatalogSubject:
    subject_id: int
    title: str
    original_title: str
    air_date: str
    subject_format: str
    episode_count: int | None
    fingerprint: str


@dataclass(frozen=True)
class CatalogEpisode:
    episode_id: int
    subject_id: int
    episode_number: int
    title: str
    air_date: str


class AnimeUpdateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            try:
                with self._connection() as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    if is_new:
                        self._create_schema(connection)
                    else:
                        self._validate_schema(connection)
            except sqlite3.Error as error:
                raise AnimeUpdateStateError(
                    f"无法初始化 anime_updates.db: {type(error).__name__}"
                ) from error
            self._initialized = True

    def set_active_subjects(self, subject_ids: set[int]) -> None:
        self.initialize()
        with self._transaction() as connection:
            connection.execute("UPDATE subjects SET active = 0")
            connection.executemany(
                "UPDATE subjects SET active = 1 WHERE subject_id = ?",
                ((subject_id,) for subject_id in sorted(subject_ids)),
            )

    def upsert_catalog(
        self,
        subject: CatalogSubject,
        episodes: list[CatalogEpisode],
        now: datetime,
    ) -> None:
        self.initialize()
        verified_at = _iso_utc(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO subjects (
                    subject_id, title, original_title, air_date,
                    subject_format, episode_count, catalog_fingerprint,
                    active, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    title = excluded.title,
                    original_title = excluded.original_title,
                    air_date = excluded.air_date,
                    subject_format = excluded.subject_format,
                    episode_count = excluded.episode_count,
                    catalog_fingerprint = excluded.catalog_fingerprint,
                    active = 1,
                    verified_at = excluded.verified_at
                """,
                (
                    subject.subject_id,
                    subject.title,
                    subject.original_title,
                    subject.air_date,
                    subject.subject_format,
                    subject.episode_count,
                    subject.fingerprint,
                    verified_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO episodes (
                    episode_id, subject_id, episode_number, title,
                    air_date, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    subject_id = excluded.subject_id,
                    episode_number = excluded.episode_number,
                    title = excluded.title,
                    air_date = excluded.air_date,
                    verified_at = excluded.verified_at
                """,
                (
                    (
                        episode.episode_id,
                        episode.subject_id,
                        episode.episode_number,
                        episode.title,
                        episode.air_date,
                        verified_at,
                    )
                    for episode in episodes
                ),
            )

    def active_subjects(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM subjects WHERE active = 1 ORDER BY subject_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def mapping_for(self, subject_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM mappings WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_mapping(
        self,
        *,
        subject_id: int,
        media_id: int,
        method: str,
        subject_fingerprint: str,
        fingerprint: str,
        now: datetime,
    ) -> None:
        self.initialize()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO mappings (
                    subject_id, media_id, method, subject_fingerprint,
                    fingerprint, valid, verified_at, error_code
                ) VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
                ON CONFLICT(subject_id) DO UPDATE SET
                    media_id = excluded.media_id,
                    method = excluded.method,
                    subject_fingerprint = excluded.subject_fingerprint,
                    fingerprint = excluded.fingerprint,
                    valid = 1,
                    verified_at = excluded.verified_at,
                    error_code = NULL
                """,
                (
                    subject_id,
                    media_id,
                    method,
                    subject_fingerprint,
                    fingerprint,
                    _iso_utc(now),
                ),
            )

    def invalidate_mapping(
        self,
        subject_id: int,
        subject_fingerprint: str,
        error_code: str,
        now: datetime,
    ) -> None:
        self.initialize()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO mappings (
                    subject_id, media_id, method, subject_fingerprint,
                    fingerprint, valid, verified_at, error_code
                ) VALUES (?, NULL, 'unresolved', ?, '', 0, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    subject_fingerprint = excluded.subject_fingerprint,
                    valid = 0,
                    verified_at = excluded.verified_at,
                    error_code = excluded.error_code
                """,
                (subject_id, subject_fingerprint, _iso_utc(now), error_code),
            )

    def episode_for_number(
        self,
        subject_id: int,
        episode_number: int,
    ) -> dict[str, Any] | None:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM episodes
                WHERE subject_id = ? AND episode_number = ?
                """,
                (subject_id, episode_number),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_schedule(
        self,
        *,
        episode_id: int,
        subject_id: int,
        media_id: int,
        episode_number: int,
        airing_at: int,
        notify_at: int,
        mapping_fingerprint: str,
        now: datetime,
    ) -> None:
        self.initialize()
        timestamp = _iso_utc(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO schedules (
                    episode_id, subject_id, media_id, episode_number,
                    airing_at, notify_at, mapping_fingerprint, state,
                    created_at, updated_at, failure_count, next_retry_at,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, 0, 0, NULL)
                ON CONFLICT(episode_id) DO UPDATE SET
                    subject_id = excluded.subject_id,
                    media_id = excluded.media_id,
                    episode_number = excluded.episode_number,
                    airing_at = excluded.airing_at,
                    notify_at = excluded.notify_at,
                    mapping_fingerprint = excluded.mapping_fingerprint,
                    updated_at = excluded.updated_at,
                    failure_count = CASE WHEN
                        schedules.airing_at != excluded.airing_at
                        OR schedules.notify_at != excluded.notify_at
                        OR schedules.mapping_fingerprint != excluded.mapping_fingerprint
                        THEN 0 ELSE schedules.failure_count END,
                    next_retry_at = CASE WHEN
                        schedules.airing_at != excluded.airing_at
                        OR schedules.notify_at != excluded.notify_at
                        OR schedules.mapping_fingerprint != excluded.mapping_fingerprint
                        THEN 0 ELSE schedules.next_retry_at END,
                    last_error = CASE WHEN
                        schedules.airing_at != excluded.airing_at
                        OR schedules.notify_at != excluded.notify_at
                        OR schedules.mapping_fingerprint != excluded.mapping_fingerprint
                        THEN NULL ELSE schedules.last_error END
                WHERE schedules.state = 'scheduled'
                """,
                (
                    episode_id,
                    subject_id,
                    media_id,
                    episode_number,
                    airing_at,
                    notify_at,
                    mapping_fingerprint,
                    timestamp,
                    timestamp,
                ),
            )

    def due_schedules(self, now_epoch: int) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT schedules.*, subjects.title, subjects.active,
                       mappings.valid AS mapping_valid,
                       mappings.fingerprint AS current_mapping_fingerprint
                FROM schedules
                JOIN subjects USING (subject_id)
                LEFT JOIN mappings USING (subject_id)
                WHERE schedules.state = 'scheduled'
                  AND schedules.notify_at <= ?
                  AND schedules.next_retry_at <= ?
                ORDER BY schedules.notify_at, schedules.episode_id
                """,
                (now_epoch, now_epoch),
            ).fetchall()
        return [dict(row) for row in rows]

    def expire_schedule(
        self,
        episode_id: int,
        now: datetime,
        *,
        expected_notify_at: int,
        expected_mapping_fingerprint: str,
    ) -> None:
        self.initialize()
        now_epoch = int(_aware_utc(now).timestamp())
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE schedules SET state = 'expired', updated_at = ?
                WHERE episode_id = ? AND state = 'scheduled'
                  AND notify_at = ? AND mapping_fingerprint = ?
                  AND (
                    ? > notify_at + ?
                    OR NOT EXISTS (
                        SELECT 1 FROM subjects
                        WHERE subjects.subject_id = schedules.subject_id
                          AND subjects.active = 1
                    )
                    OR NOT EXISTS (
                        SELECT 1 FROM mappings
                        WHERE mappings.subject_id = schedules.subject_id
                          AND mappings.valid = 1
                          AND mappings.fingerprint = schedules.mapping_fingerprint
                    )
                  )
                """,
                (
                    _iso_utc(now),
                    episode_id,
                    expected_notify_at,
                    expected_mapping_fingerprint,
                    now_epoch,
                    RECOVERY_WINDOW_SECONDS,
                ),
            )

    def suppress_schedule(self, episode_id: int, now: datetime) -> None:
        self.initialize()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE schedules SET state = 'suppressed', updated_at = ?
                WHERE episode_id = ? AND state = 'scheduled'
                """,
                (_iso_utc(now), episode_id),
            )

    def create_pending(
        self,
        episode_id: int,
        payload: dict[str, object],
        now: datetime,
        *,
        expected_airing_at: int,
        expected_notify_at: int,
        expected_mapping_fingerprint: str,
    ) -> bool:
        self.initialize()
        event_id = f"episode:{episode_id}"
        if payload.get("event_id") != event_id:
            raise AnimeUpdateStateError("pending 事件 event_id 与 episode 不匹配")
        created_at = _iso_utc(now)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT state, airing_at, notify_at, mapping_fingerprint
                FROM schedules WHERE episode_id = ?
                """,
                (episode_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "scheduled"
                or row["airing_at"] != expected_airing_at
                or row["notify_at"] != expected_notify_at
                or row["mapping_fingerprint"] != expected_mapping_fingerprint
            ):
                return False
            connection.execute(
                """
                INSERT INTO events (
                    event_id, episode_id, payload_json, state,
                    created_at, acked_at
                ) VALUES (?, ?, ?, 'pending', ?, NULL)
                """,
                (event_id, episode_id, encoded, created_at),
            )
            connection.execute(
                """
                UPDATE schedules
                SET state = 'pending', updated_at = ?, last_error = NULL
                WHERE episode_id = ? AND state = 'scheduled'
                """,
                (created_at, episode_id),
            )
        return True

    def record_due_failure(
        self,
        episode_id: int,
        *,
        next_retry_at: int,
        error_code: str,
        now: datetime,
        expected_notify_at: int,
        expected_mapping_fingerprint: str,
    ) -> None:
        self.initialize()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE schedules
                SET failure_count = failure_count + 1,
                    next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE episode_id = ? AND state = 'scheduled'
                  AND notify_at = ? AND mapping_fingerprint = ?
                """,
                (
                    next_retry_at,
                    error_code,
                    _iso_utc(now),
                    episode_id,
                    expected_notify_at,
                    expected_mapping_fingerprint,
                ),
            )

    def fetch_pending(self, offset: int, limit: int) -> list[dict[str, object]]:
        _bounded_int(offset, "offset", minimum=0)
        _bounded_int(limit, "limit", minimum=1, maximum=50)
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, payload_json FROM events
                WHERE state = 'pending'
                ORDER BY created_at, event_id
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise AnimeUpdateStateError("pending 事件 payload 已损坏") from error
            if not isinstance(payload, dict):
                raise AnimeUpdateStateError("pending 事件 payload 不是对象")
            if payload.get("event_id") != row["event_id"]:
                raise AnimeUpdateStateError("pending 事件 event_id 已损坏")
            result.append(payload)
        return result

    def acknowledge(self, event_ids: list[str], now: datetime) -> int:
        clean_ids = _validate_event_ids(event_ids)
        if not clean_ids:
            return 0
        self.initialize()
        acked_at = _iso_utc(now)
        transitioned = 0
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT events.event_id, events.episode_id, "
                f"events.state, schedules.state AS schedule_state "
                f"FROM events JOIN schedules USING (episode_id) "
                f"WHERE event_id IN ({','.join('?' for _ in clean_ids)})",
                clean_ids,
            ).fetchall()
            by_id = {str(row["event_id"]): row for row in rows}
            invalid = [
                event_id
                for event_id in clean_ids
                if event_id not in by_id
                or by_id[event_id]["state"] not in {"pending", "acked"}
                or by_id[event_id]["schedule_state"] != by_id[event_id]["state"]
            ]
            if invalid:
                raise AnimeUpdateStateError("ACK 包含未知或不可确认的 event_id")
            for event_id in clean_ids:
                row = by_id[event_id]
                if row["state"] == "acked":
                    continue
                connection.execute(
                    """
                    UPDATE events SET state = 'acked', acked_at = ?
                    WHERE event_id = ? AND state = 'pending'
                    """,
                    (acked_at, event_id),
                )
                connection.execute(
                    """
                    UPDATE schedules SET state = 'acked', updated_at = ?
                    WHERE episode_id = ? AND state = 'pending'
                    """,
                    (acked_at, int(row["episode_id"])),
                )
                transitioned += 1
        return transitioned

    def record_poll(
        self,
        kind: str,
        *,
        success: bool,
        next_run_at: int,
        error_code: str | None,
        now: datetime,
    ) -> None:
        self.initialize()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO poll_state (
                    kind, last_success_at, last_failure_at,
                    failure_count, next_run_at, error_code
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind) DO UPDATE SET
                    last_success_at = CASE WHEN excluded.error_code IS NULL
                        THEN excluded.last_success_at
                        ELSE poll_state.last_success_at END,
                    last_failure_at = CASE WHEN excluded.error_code IS NOT NULL
                        THEN excluded.last_failure_at
                        ELSE poll_state.last_failure_at END,
                    failure_count = CASE WHEN excluded.error_code IS NULL
                        THEN 0 ELSE poll_state.failure_count + 1 END,
                    next_run_at = excluded.next_run_at,
                    error_code = excluded.error_code
                """,
                (
                    kind,
                    _iso_utc(now) if success else None,
                    None if success else _iso_utc(now),
                    0 if success else 1,
                    next_run_at,
                    error_code,
                ),
            )

    def poll_failure_count(self, kind: str) -> int:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT failure_count FROM poll_state WHERE kind = ?",
                (kind,),
            ).fetchone()
        return int(row["failure_count"]) if row is not None else 0

    def poll_next_run_at(self, kind: str) -> int:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT next_run_at FROM poll_state WHERE kind = ?",
                (kind,),
            ).fetchone()
        return int(row["next_run_at"]) if row is not None else 0

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata(key, value) VALUES ('schema_version', '1');

            CREATE TABLE subjects (
                subject_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                original_title TEXT NOT NULL,
                air_date TEXT NOT NULL,
                subject_format TEXT NOT NULL,
                episode_count INTEGER,
                catalog_fingerprint TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                verified_at TEXT NOT NULL
            );
            CREATE TABLE episodes (
                episode_id INTEGER PRIMARY KEY,
                subject_id INTEGER NOT NULL REFERENCES subjects(subject_id),
                episode_number INTEGER NOT NULL CHECK(episode_number > 0),
                title TEXT NOT NULL,
                air_date TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                UNIQUE(subject_id, episode_number)
            );
            CREATE TABLE mappings (
                subject_id INTEGER PRIMARY KEY REFERENCES subjects(subject_id),
                media_id INTEGER,
                method TEXT NOT NULL,
                subject_fingerprint TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
                verified_at TEXT NOT NULL,
                error_code TEXT
            );
            CREATE TABLE schedules (
                episode_id INTEGER PRIMARY KEY REFERENCES episodes(episode_id),
                subject_id INTEGER NOT NULL REFERENCES subjects(subject_id),
                media_id INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                airing_at INTEGER NOT NULL,
                notify_at INTEGER NOT NULL,
                mapping_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK(
                    state IN ('scheduled', 'pending', 'acked', 'suppressed', 'expired')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                episode_id INTEGER NOT NULL UNIQUE REFERENCES schedules(episode_id),
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'acked')),
                created_at TEXT NOT NULL,
                acked_at TEXT
            );
            CREATE TABLE poll_state (
                kind TEXT PRIMARY KEY,
                last_success_at TEXT,
                last_failure_at TEXT,
                failure_count INTEGER NOT NULL,
                next_run_at INTEGER NOT NULL,
                error_code TEXT
            );
            CREATE INDEX schedules_due_idx
                ON schedules(state, notify_at, next_retry_at);
            CREATE INDEX events_pending_idx
                ON events(state, created_at, event_id);
            """
        )
        connection.commit()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise AnimeUpdateStateError("anime_updates.db 完整性检查失败")
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error as error:
            raise AnimeUpdateStateError("anime_updates.db 缺少 schema metadata") from error
        if row is None or row[0] != SCHEMA_VERSION:
            raise AnimeUpdateStateError("anime_updates.db schema version 不受支持")
        required = {
            "metadata",
            "subjects",
            "episodes",
            "mappings",
            "schedules",
            "events",
            "poll_state",
        }
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not required.issubset(present):
            raise AnimeUpdateStateError("anime_updates.db schema 不完整")


class AnimeUpdateCoordinator:
    def __init__(
        self,
        store: AnimeUpdateStore,
        bangumi: BangumiApi,
        anilist: AniListApi,
        config: AnimePushRuntimeConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.store = store
        self.bangumi = bangumi
        self.anilist = anilist
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter or (
            lambda seconds: seconds * random.uniform(0.9, 1.1)
        )

    async def run(self) -> None:
        current_monotonic = monotonic()
        current_epoch = int(_aware_utc(self._clock()).timestamp())
        deadlines = {
            kind: current_monotonic
            + max(0, self.store.poll_next_run_at(kind) - current_epoch)
            for kind in ("catalog", "schedule", "due")
        }
        methods = {
            "catalog": self.refresh_catalog,
            "schedule": self.refresh_schedules,
            "due": self.evaluate_due,
        }
        intervals = {
            "catalog": CATALOG_INTERVAL_SECONDS,
            "schedule": SCHEDULE_INTERVAL_SECONDS,
            "due": DUE_INTERVAL_SECONDS,
        }
        while True:
            current = monotonic()
            for kind in ("catalog", "schedule", "due"):
                if current < deadlines[kind]:
                    continue
                delay = await self._run_step(kind, methods[kind], intervals[kind])
                deadlines[kind] = monotonic() + delay
            wait = max(0.1, min(deadlines.values()) - monotonic())
            await asyncio.sleep(wait)

    def refresh_catalog(self) -> None:
        now = _aware_utc(self._clock())
        me = self.bangumi.get_me()
        username = me.get("username")
        if not isinstance(username, str) or not username.strip():
            raise BangumiApiError("Bangumi 当前用户缺少 username")
        items = self._watching_collections(username.strip())
        active_ids = {_positive_int(item.get("subject_id"), "subject_id") for item in items}
        self.store.set_active_subjects(active_ids)
        first_failure: Exception | None = None
        for subject_id in sorted(active_ids):
            try:
                raw_subject = self.bangumi.get_subject(subject_id)
                raw_episodes = self.bangumi.list_episodes(subject_id, episode_type=0)
                subject, episodes = _catalog_records(raw_subject, raw_episodes)
                if subject.subject_id != subject_id:
                    raise BangumiApiError("Bangumi 条目 ID 与请求不匹配")
                self.store.upsert_catalog(subject, episodes, now)
            except Exception as error:
                if _is_auth_or_rate_limit(error):
                    raise
                if first_failure is None:
                    first_failure = error
                logger.warning(
                    "[anime_push.catalog] subject 刷新失败 subject_id=%s error=%s",
                    subject_id,
                    _error_code(error),
                )
        if first_failure is not None:
            raise first_failure

    def refresh_schedules(self) -> None:
        now = _aware_utc(self._clock())
        now_epoch = int(now.timestamp())
        first_failure: Exception | None = None
        for subject in self.store.active_subjects():
            subject_id = int(subject["subject_id"])
            try:
                media, method = self._resolve_media(subject)
                media_id = _positive_int(
                    media.get("id"),
                    "AniList media id",
                    AniListApiError,
                )
                mapping_fingerprint = _mapping_fingerprint(subject, media)
                self.store.save_mapping(
                    subject_id=subject_id,
                    media_id=media_id,
                    method=method,
                    subject_fingerprint=str(subject["catalog_fingerprint"]),
                    fingerprint=mapping_fingerprint,
                    now=now,
                )
                next_airing = media.get("nextAiringEpisode")
                if next_airing is None:
                    continue
                if not isinstance(next_airing, dict):
                    raise AniListApiError("AniList nextAiringEpisode 不是对象")
                episode_number = _positive_int(
                    next_airing.get("episode"),
                    "AniList episode",
                    AniListApiError,
                )
                airing_at = _positive_int(
                    next_airing.get("airingAt"),
                    "AniList airingAt",
                    AniListApiError,
                )
                if airing_at <= now_epoch:
                    continue
                episode = self.store.episode_for_number(subject_id, episode_number)
                if episode is None:
                    self.store.invalidate_mapping(
                        subject_id,
                        str(subject["catalog_fingerprint"]),
                        "episode_mapping_missing",
                        now,
                    )
                    continue
                notify_at = (
                    airing_at - airing_at % 60
                    - self.config.notify_before_minutes * 60
                )
                self.store.upsert_schedule(
                    episode_id=int(episode["episode_id"]),
                    subject_id=subject_id,
                    media_id=media_id,
                    episode_number=episode_number,
                    airing_at=airing_at,
                    notify_at=notify_at,
                    mapping_fingerprint=mapping_fingerprint,
                    now=now,
                )
            except AnimeMappingError as error:
                logger.warning(
                    "[anime_push.schedule] subject 映射跳过 subject_id=%s error=%s",
                    subject_id,
                    _error_code(error),
                )
            except Exception as error:
                if _is_auth_or_rate_limit(error):
                    raise
                if first_failure is None:
                    first_failure = error
                logger.warning(
                    "[anime_push.schedule] subject 刷新失败 subject_id=%s error=%s",
                    subject_id,
                    _error_code(error),
                )
        if first_failure is not None:
            raise first_failure

    def evaluate_due(self) -> None:
        now = _aware_utc(self._clock())
        now_epoch = int(now.timestamp())
        for schedule in self.store.due_schedules(now_epoch):
            episode_id = int(schedule["episode_id"])
            if (
                now_epoch > int(schedule["notify_at"]) + RECOVERY_WINDOW_SECONDS
                or int(schedule["active"]) != 1
                or schedule["mapping_valid"] != 1
                or schedule["mapping_fingerprint"]
                != schedule["current_mapping_fingerprint"]
            ):
                self.store.expire_schedule(
                    episode_id,
                    now,
                    expected_notify_at=int(schedule["notify_at"]),
                    expected_mapping_fingerprint=str(
                        schedule["mapping_fingerprint"]
                    ),
                )
                continue
            try:
                collection = self.bangumi.get_episode_collection(episode_id)
                _validate_episode_collection(collection, episode_id)
                collection_type = int(collection["type"])
                if collection_type == 2:
                    self.store.suppress_schedule(episode_id, now)
                    continue
                payload = _event_payload(schedule, self.config.display_timezone)
                self.store.create_pending(
                    episode_id,
                    payload,
                    now,
                    expected_airing_at=int(schedule["airing_at"]),
                    expected_notify_at=int(schedule["notify_at"]),
                    expected_mapping_fingerprint=str(
                        schedule["mapping_fingerprint"]
                    ),
                )
            except Exception as error:
                delay = _retry_delay(
                    error,
                    int(schedule["failure_count"]),
                    self._jitter,
                )
                self.store.record_due_failure(
                    episode_id,
                    next_retry_at=now_epoch + int(delay),
                    error_code=_error_code(error),
                    now=now,
                    expected_notify_at=int(schedule["notify_at"]),
                    expected_mapping_fingerprint=str(
                        schedule["mapping_fingerprint"]
                    ),
                )
                logger.warning(
                    "[anime_push.due] 单集复核失败 episode_id=%s error=%s",
                    episode_id,
                    _error_code(error),
                )

    def _watching_collections(self, username: str) -> list[dict[str, Any]]:
        offset = 0
        expected_total: int | None = None
        result: list[dict[str, Any]] = []
        while True:
            page = self.bangumi.list_collections(
                username,
                subject_type=2,
                collection_type=3,
                limit=50,
                offset=offset,
            )
            total = _nonnegative_int(page.get("total"), "收藏 total")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise BangumiApiError("Bangumi 收藏分页 total 在读取中发生变化")
            data = page.get("data")
            if not isinstance(data, list):
                raise BangumiApiError("Bangumi 收藏分页 data 不是数组")
            batch = [_object(item, "收藏条目", BangumiApiError) for item in data]
            result.extend(batch)
            offset += len(batch)
            if offset >= total:
                return result
            if not batch:
                raise BangumiApiError("Bangumi 收藏分页提前结束")

    def _resolve_media(
        self,
        subject: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        subject_id = int(subject["subject_id"])
        override = self.config.media_id_overrides.get(subject_id)
        if override is not None:
            try:
                media = self.anilist.get_anime(override)
            except AniListNotFoundError as error:
                self.store.invalidate_mapping(
                    subject_id,
                    str(subject["catalog_fingerprint"]),
                    "override_not_found",
                    _aware_utc(self._clock()),
                )
                raise AnimeMappingError("AniList 显式覆盖不存在") from error
            try:
                _validate_anime_media(media)
            except AniListApiError as error:
                self.store.invalidate_mapping(
                    subject_id,
                    str(subject["catalog_fingerprint"]),
                    "override_invalid",
                    _aware_utc(self._clock()),
                )
                raise AnimeMappingError("AniList 显式覆盖不是动画") from error
            return media, "override"

        existing = self.store.mapping_for(subject_id)
        if existing is not None and existing.get("valid") == 1:
            media_id = existing.get("media_id")
            if isinstance(media_id, int) and media_id > 0:
                try:
                    media = self.anilist.get_anime(media_id)
                except AniListNotFoundError:
                    media = None
                if media is not None and _candidate_matches(subject, media):
                    return media, "automatic"

        candidates = self.anilist.search_anime(str(subject["original_title"]))
        matches = [media for media in candidates if _candidate_matches(subject, media)]
        if len(matches) != 1:
            self.store.invalidate_mapping(
                subject_id,
                str(subject["catalog_fingerprint"]),
                "ambiguous" if len(matches) > 1 else "not_found",
                _aware_utc(self._clock()),
            )
            raise AnimeMappingError("AniList 自动映射没有唯一严格候选")
        return matches[0], "automatic"

    async def _run_step(
        self,
        kind: str,
        method: Callable[[], None],
        interval: int,
    ) -> float:
        try:
            await _run_sync_to_completion(method)
        except Exception as error:
            now = _aware_utc(self._clock())
            delay = _retry_delay(
                error,
                self.store.poll_failure_count(kind),
                self._jitter,
            )
            self.store.record_poll(
                kind,
                success=False,
                next_run_at=int(now.timestamp() + delay),
                error_code=_error_code(error),
                now=now,
            )
            logger.warning(
                "[anime_push.refresh] 刷新失败 kind=%s error=%s",
                kind,
                _error_code(error),
            )
            return delay
        now = _aware_utc(self._clock())
        delay = (
            float(interval)
            if kind == "due"
            else max(1.0, self._jitter(float(interval)))
        )
        self.store.record_poll(
            kind,
            success=True,
            next_run_at=int(now.timestamp() + delay),
            error_code=None,
            now=now,
        )
        return delay


async def _run_sync_to_completion(method: Callable[[], None]) -> None:
    work = asyncio.create_task(asyncio.to_thread(method))
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        await work
        raise


def _catalog_records(
    raw_subject: dict[str, Any],
    raw_episodes: list[dict[str, Any]],
) -> tuple[CatalogSubject, list[CatalogEpisode]]:
    subject_id = _positive_int(raw_subject.get("id"), "Bangumi subject id")
    original_title = _required_text(raw_subject.get("name"), "Bangumi 原文名")
    translated = raw_subject.get("name_cn")
    title = (
        translated.strip()
        if isinstance(translated, str) and translated.strip()
        else original_title
    )
    air_date = _optional_text(raw_subject.get("date"))
    subject_format = _optional_text(raw_subject.get("platform"))
    episode_count = _optional_positive_int(raw_subject.get("eps"), "Bangumi eps")
    subject_payload = {
        "subject_id": subject_id,
        "original_title": original_title,
        "air_date": air_date,
        "subject_format": subject_format,
        "episode_count": episode_count,
    }
    subject = CatalogSubject(
        subject_id=subject_id,
        title=title,
        original_title=original_title,
        air_date=air_date,
        subject_format=subject_format,
        episode_count=episode_count,
        fingerprint=_fingerprint(subject_payload),
    )

    episodes: list[CatalogEpisode] = []
    seen_numbers: set[int] = set()
    for raw_episode in raw_episodes:
        episode_id = _positive_int(raw_episode.get("id"), "Bangumi episode id")
        episode_type = _nonnegative_int(raw_episode.get("type"), "Bangumi episode type")
        if episode_type != 0:
            raise BangumiApiError("Bangumi 章节目录包含非本篇章节")
        number = _episode_number(raw_episode.get("sort"))
        if number in seen_numbers:
            raise BangumiApiError("Bangumi 本篇章节序号重复")
        seen_numbers.add(number)
        name_cn = raw_episode.get("name_cn")
        name = raw_episode.get("name")
        title_value = name_cn if isinstance(name_cn, str) and name_cn.strip() else name
        episodes.append(
            CatalogEpisode(
                episode_id=episode_id,
                subject_id=subject_id,
                episode_number=number,
                title=_optional_text(title_value),
                air_date=_optional_text(raw_episode.get("airdate")),
            )
        )
    return subject, episodes


def _candidate_matches(subject: Mapping[str, Any], media: dict[str, Any]) -> bool:
    try:
        _validate_anime_media(media)
        titles = _object(media.get("title"), "AniList title", AniListApiError)
        native = titles.get("native")
        if not isinstance(native, str) or _normalize_title(native) != _normalize_title(
            str(subject["original_title"])
        ):
            return False

        air_date = str(subject.get("air_date") or "")
        if len(air_date) >= 7:
            start = _object(
                media.get("startDate"),
                "AniList startDate",
                AniListApiError,
            )
            year = _positive_int(
                start.get("year"),
                "AniList start year",
                AniListApiError,
            )
            month = _positive_int(
                start.get("month"),
                "AniList start month",
                AniListApiError,
            )
            if year != int(air_date[:4]) or _season(month) != media.get("season"):
                return False

        expected_format = _anilist_format(str(subject.get("subject_format") or ""))
        if expected_format is not None and media.get("format") != expected_format:
            return False

        episode_count = subject.get("episode_count")
        media_episodes = media.get("episodes")
        if (
            isinstance(episode_count, int)
            and episode_count > 0
            and media_episodes is not None
            and _positive_int(
                media_episodes,
                "AniList episodes",
                AniListApiError,
            )
            != episode_count
        ):
            return False
    except (AniListApiError, ValueError, TypeError):
        return False
    return True


def _validate_anime_media(media: dict[str, Any]) -> None:
    _positive_int(media.get("id"), "AniList media id", AniListApiError)
    if media.get("type") != "ANIME":
        raise AniListApiError("AniList Media 不是 anime")


def _validate_episode_collection(collection: dict[str, Any], episode_id: int) -> None:
    episode = _object(collection.get("episode"), "章节收藏 episode", BangumiApiError)
    if _positive_int(episode.get("id"), "章节收藏 episode id") != episode_id:
        raise BangumiApiError("Bangumi 单集章节收藏 ID 与请求不匹配")
    collection_type = collection.get("type")
    if isinstance(collection_type, bool) or collection_type not in {0, 1, 2, 3}:
        raise BangumiApiError("Bangumi 单集章节收藏 type 无效")
    updated_at = collection.get("updated_at")
    if isinstance(updated_at, bool) or not isinstance(updated_at, int):
        raise BangumiApiError("Bangumi 单集章节收藏 updated_at 无效")


def _event_payload(
    schedule: Mapping[str, Any],
    display_timezone: str,
) -> dict[str, object]:
    airing_at = int(schedule["airing_at"])
    display_time = datetime.fromtimestamp(airing_at, timezone.utc).astimezone(
        ZoneInfo(display_timezone)
    )
    title = str(schedule["title"])
    episode_number = int(schedule["episode_number"])
    local_time = display_time.strftime("%Y-%m-%d %H:%M")
    return {
        "event_id": f"episode:{int(schedule['episode_id'])}",
        "kind": "alert",
        "source_type": "anime_schedule",
        "source_name": "Bangumi × AniList",
        "title": f"《{title}》第 {episode_number} 集计划于 {display_time:%H:%M} 放送",
        "content": (
            f"计划放送时间：{local_time}（{display_timezone}）。"
            "这是放送提醒，不代表任何平台已经上线。"
        ),
        "severity": "low",
        "scheduled_at": datetime.fromtimestamp(airing_at, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _mapping_fingerprint(
    subject: Mapping[str, Any],
    media: Mapping[str, Any],
) -> str:
    return _fingerprint(
        {
            "subject_fingerprint": subject["catalog_fingerprint"],
            "media_id": media.get("id"),
            "type": media.get("type"),
            "format": media.get("format"),
            "episodes": media.get("episodes"),
            "season": media.get("season"),
            "seasonYear": media.get("seasonYear"),
            "startDate": media.get("startDate"),
            "title": media.get("title"),
        }
    )


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith(("P", "Z"))
    )


def _anilist_format(value: str) -> str | None:
    normalized = value.strip().upper()
    return {
        "TV": "TV",
        "WEB": "ONA",
        "ONA": "ONA",
        "OVA": "OVA",
        "MOVIE": "MOVIE",
        "剧场版": "MOVIE",
    }.get(normalized)


def _season(month: int) -> str:
    if month <= 3:
        return "WINTER"
    if month <= 6:
        return "SPRING"
    if month <= 9:
        return "SUMMER"
    return "FALL"


def _retry_delay(
    error: BaseException,
    failure_count: int,
    jitter: Callable[[float], float],
) -> float:
    status = getattr(error, "status_code", None)
    if status in {401, 403}:
        return 24 * 60 * 60
    retry_after = getattr(error, "retry_after_seconds", None)
    if status == 429 and isinstance(retry_after, (int, float)) and retry_after >= 0:
        return float(retry_after)
    base = min(BASE_RETRY_SECONDS * 2 ** min(failure_count, 5), MAX_RETRY_SECONDS)
    return max(1.0, jitter(float(base)))


def _is_auth_or_rate_limit(error: BaseException) -> bool:
    return getattr(error, "status_code", None) in {401, 403, 429}


def _error_code(error: BaseException) -> str:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return f"http_{status}"
    return type(error).__name__


def _validate_event_ids(event_ids: list[str]) -> list[str]:
    if not isinstance(event_ids, list):
        raise ValueError("event_ids 必须是数组")
    result: list[str] = []
    seen: set[str] = set()
    for value in event_ids:
        if not isinstance(value, str):
            raise ValueError("event_id 必须是字符串")
        clean = value.strip()
        prefix, separator, suffix = clean.partition(":")
        if prefix != "episode" or separator != ":" or not suffix.isdigit():
            raise ValueError("event_id 必须使用 episode:<id>")
        if int(suffix) <= 0:
            raise ValueError("event_id 中的 episode ID 必须是正整数")
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _object(
    value: object,
    label: str,
    error_type: type[RuntimeError],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error_type(f"{label} 不是对象")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BangumiApiError(f"{label} 无效")
    return value.strip()


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(
    value: object,
    label: str,
    error_type: type[RuntimeError] = BangumiApiError,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise error_type(f"{label} 必须是正整数")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BangumiApiError(f"{label} 必须是非负整数")
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None or value == 0:
        return None
    return _positive_int(value, label)


def _episode_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise BangumiApiError("Bangumi 本篇章节序号无效")
    number = int(value)
    if number <= 0 or float(value) != number:
        raise BangumiApiError("Bangumi 本篇章节序号必须是正整数")
    return number


def _bounded_int(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label} 超出允许范围")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")
