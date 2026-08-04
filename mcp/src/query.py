from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Literal


CollectionQueryOperation = Literal["list_all", "filter", "analyze", "continue"]
CollectionQueryRequestOperation = Literal["list_all", "filter", "analyze"]


class QueryStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedCollectionQuery:
    operation: CollectionQueryOperation
    username: str
    subject_type: str
    status: str
    candidate_total: int
    confirmation_text: str
    min_rating: int | None = None
    max_rating: int | None = None
    requested_count: int = 10
    return_all_matches: bool = False
    query_id: str | None = None


@dataclass(frozen=True)
class PendingQueryConfirmation:
    confirmation_id: str
    plan: PreparedCollectionQuery
    expires_at: float


class QueryConfirmationStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 600,
        max_pending: int = 32,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_pending = max_pending
        self._clock = clock
        self._pending: dict[str, PendingQueryConfirmation] = {}
        self._lock = threading.Lock()

    def prepare(self, plan: PreparedCollectionQuery) -> PendingQueryConfirmation:
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            if len(self._pending) >= self._max_pending:
                oldest = min(
                    self._pending,
                    key=lambda key: self._pending[key].expires_at,
                )
                del self._pending[oldest]
            confirmation_id = secrets.token_urlsafe(18)
            pending = PendingQueryConfirmation(
                confirmation_id=confirmation_id,
                plan=plan,
                expires_at=now + self._ttl_seconds,
            )
            self._pending[confirmation_id] = pending
            return pending

    def consume(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> PreparedCollectionQuery:
        with self._lock:
            now = self._clock()
            pending = self._pending.get(confirmation_id)
            if pending is None:
                raise QueryStateError("查询确认记录不存在或已经使用")
            if pending.expires_at <= now:
                del self._pending[confirmation_id]
                raise QueryStateError("查询确认已过期，请重新预览")
            expected = pending.plan.confirmation_text.encode("utf-8")
            actual = confirmation_text.strip().encode("utf-8")
            if not secrets.compare_digest(expected, actual):
                raise QueryStateError("查询确认文字不匹配，请逐字使用预览文字")
            del self._pending[confirmation_id]
            return pending.plan

    def _purge_expired(self, now: float) -> None:
        for key in [
            key
            for key, pending in self._pending.items()
            if pending.expires_at <= now
        ]:
            del self._pending[key]


@dataclass
class CollectionQuerySession:
    query_id: str
    username: str
    subject_type: str
    status: str
    total: int
    source_total: int
    next_offset: int
    read_count: int
    displayed_count: int
    seen_subject_ids: set[int]
    expires_at: float
    authorized_large_read: bool = False
    source_complete: bool = False
    buffer: list[dict[str, object]] = field(default_factory=list)
    busy: bool = False


@dataclass(frozen=True)
class ContinueAction:
    kind: Literal["page", "confirm", "fetch"]
    query_id: str
    username: str
    subject_type: str
    status: str
    total: int
    source_total: int
    displayed_count: int
    read_count: int
    offset: int = 0
    limit: int = 0
    items: tuple[dict[str, object], ...] = ()


class CollectionQuerySessionStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 1800,
        max_sessions: int = 32,
        confirmation_threshold: int = 100,
        display_page_size: int = 10,
        bulk_page_size: int = 50,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._confirmation_threshold = confirmation_threshold
        self._display_page_size = display_page_size
        self._bulk_page_size = bulk_page_size
        self._clock = clock
        self._sessions: dict[str, CollectionQuerySession] = {}
        self._lock = threading.Lock()

    def create_page_session(
        self,
        *,
        username: str,
        subject_type: str,
        status: str,
        total: int,
        items: list[dict[str, object]],
    ) -> tuple[str | None, float | None]:
        if len(items) >= total:
            return None, None
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            self._evict_if_full()
            query_id = secrets.token_urlsafe(18)
            self._sessions[query_id] = CollectionQuerySession(
                query_id=query_id,
                username=username,
                subject_type=subject_type,
                status=status,
                total=total,
                source_total=total,
                next_offset=len(items),
                read_count=len(items),
                displayed_count=len(items),
                seen_subject_ids=_subject_ids(items),
                expires_at=now + self._ttl_seconds,
            )
            return query_id, self._sessions[query_id].expires_at

    def create_result_session(
        self,
        *,
        username: str,
        subject_type: str,
        status: str,
        source_total: int,
        items: list[dict[str, object]],
        initial_count: int,
    ) -> tuple[str | None, float | None, list[dict[str, object]]]:
        first = items[:initial_count]
        remaining = items[initial_count:]
        if not remaining:
            return None, None, first
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            self._evict_if_full()
            query_id = secrets.token_urlsafe(18)
            self._sessions[query_id] = CollectionQuerySession(
                query_id=query_id,
                username=username,
                subject_type=subject_type,
                status=status,
                total=len(items),
                source_total=source_total,
                next_offset=source_total,
                read_count=source_total,
                displayed_count=len(first),
                seen_subject_ids=_subject_ids(items),
                expires_at=now + self._ttl_seconds,
                authorized_large_read=True,
                source_complete=True,
                buffer=list(remaining),
            )
            return query_id, self._sessions[query_id].expires_at, first

    def next_action(self, query_id: str, username: str) -> ContinueAction:
        with self._lock:
            session = self._get(query_id, username)
            if session.busy:
                raise QueryStateError("查询会话正在读取，请勿并发继续")
            session.expires_at = self._clock() + self._ttl_seconds
            if session.buffer:
                items = self._take_buffered_page(session)
                return self._page_action(session, items)
            if session.source_complete or session.next_offset >= session.source_total:
                del self._sessions[query_id]
                raise QueryStateError("查询会话已经没有后续结果")
            next_count = min(
                self._display_page_size,
                session.source_total - session.read_count,
            )
            if (
                not session.authorized_large_read
                and session.read_count + next_count >= self._confirmation_threshold
            ):
                return ContinueAction(
                    kind="confirm",
                    query_id=session.query_id,
                    username=session.username,
                    subject_type=session.subject_type,
                    status=session.status,
                    total=session.total,
                    source_total=session.source_total,
                    displayed_count=session.displayed_count,
                    read_count=session.read_count,
                )
            session.busy = True
            return ContinueAction(
                kind="fetch",
                query_id=session.query_id,
                username=session.username,
                subject_type=session.subject_type,
                status=session.status,
                total=session.total,
                source_total=session.source_total,
                displayed_count=session.displayed_count,
                read_count=session.read_count,
                offset=session.next_offset,
                limit=(
                    self._bulk_page_size
                    if session.authorized_large_read
                    else self._display_page_size
                ),
            )

    def authorize(self, query_id: str, username: str) -> None:
        with self._lock:
            session = self._get(query_id, username)
            if session.authorized_large_read:
                raise QueryStateError("该查询会话已经完成大量读取授权")
            session.authorized_large_read = True
            session.expires_at = self._clock() + self._ttl_seconds

    def finish_fetch(
        self,
        action: ContinueAction,
        *,
        total: int,
        items: list[dict[str, object]],
    ) -> ContinueAction:
        with self._lock:
            session = self._get(action.query_id, action.username)
            if not session.busy or session.next_offset != action.offset:
                raise QueryStateError("查询会话读取状态已变化")
            try:
                if total != session.source_total:
                    raise QueryStateError("查询期间 Bangumi 收藏总数发生变化")
                ids = _subject_ids(items)
                if ids & session.seen_subject_ids:
                    raise QueryStateError("查询期间 Bangumi 分页出现重复条目")
                session.seen_subject_ids.update(ids)
                session.next_offset += len(items)
                session.read_count += len(items)
                session.buffer.extend(items)
                session.expires_at = self._clock() + self._ttl_seconds
                page = self._take_buffered_page(session)
                return self._page_action(session, page)
            finally:
                session.busy = False

    def cancel_fetch(self, action: ContinueAction) -> None:
        with self._lock:
            session = self._sessions.get(action.query_id)
            if session is not None and session.username == action.username:
                session.busy = False

    def _page_action(
        self,
        session: CollectionQuerySession,
        items: list[dict[str, object]],
    ) -> ContinueAction:
        return ContinueAction(
            kind="page",
            query_id=session.query_id,
            username=session.username,
            subject_type=session.subject_type,
            status=session.status,
            total=session.total,
            source_total=session.source_total,
            displayed_count=session.displayed_count,
            read_count=session.read_count,
            items=tuple(items),
        )

    def _take_buffered_page(
        self,
        session: CollectionQuerySession,
    ) -> list[dict[str, object]]:
        items = session.buffer[: self._display_page_size]
        del session.buffer[: self._display_page_size]
        session.displayed_count += len(items)
        return items

    def _get(self, query_id: str, username: str) -> CollectionQuerySession:
        now = self._clock()
        self._purge_expired(now)
        session = self._sessions.get(query_id)
        if session is None or session.username != username:
            raise QueryStateError("查询会话不存在或已过期")
        return session

    def _evict_if_full(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return
        oldest = min(
            self._sessions,
            key=lambda key: self._sessions[key].expires_at,
        )
        del self._sessions[oldest]

    def _purge_expired(self, now: float) -> None:
        for key in [
            key
            for key, session in self._sessions.items()
            if session.expires_at <= now
        ]:
            del self._sessions[key]


def _subject_ids(items: list[dict[str, object]]) -> set[int]:
    result: set[int] = set()
    for item in items:
        subject = item.get("subject")
        if not isinstance(subject, dict):
            raise QueryStateError("查询结果缺少 subject")
        subject_id = subject.get("id")
        if not isinstance(subject_id, int) or isinstance(subject_id, bool):
            raise QueryStateError("查询结果 subject id 无效")
        if subject_id in result:
            raise QueryStateError("查询结果出现重复条目")
        result.add(subject_id)
    return result
