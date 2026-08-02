from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal


class ConfirmationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedUpdate:
    kind: Literal["collection_status", "anime_progress"]
    subject_id: int
    subject_title: str
    target_label: str
    confirmation_text: str
    collection_type: int | None = None
    episode_number: int | None = None
    target_episode_id: int | None = None


@dataclass(frozen=True)
class PendingConfirmation:
    confirmation_id: str
    operation: PreparedUpdate
    expires_at: float


class ConfirmationStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()

    def prepare(self, operation: PreparedUpdate) -> PendingConfirmation:
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            confirmation_id = secrets.token_urlsafe(18)
            pending = PendingConfirmation(
                confirmation_id=confirmation_id,
                operation=operation,
                expires_at=now + self._ttl_seconds,
            )
            self._pending[confirmation_id] = pending
            return pending

    def consume(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> PreparedUpdate:
        with self._lock:
            now = self._clock()
            pending = self._pending.get(confirmation_id)
            if pending is None:
                raise ConfirmationError("确认记录不存在或已经使用")
            if pending.expires_at <= now:
                del self._pending[confirmation_id]
                raise ConfirmationError("确认记录已过期，请重新预览")
            expected = pending.operation.confirmation_text.encode("utf-8")
            actual = confirmation_text.strip().encode("utf-8")
            if not secrets.compare_digest(expected, actual):
                raise ConfirmationError("确认文字不匹配，请逐字使用预览给出的确认文字")
            del self._pending[confirmation_id]
            return pending.operation

    def _purge_expired(self, now: float) -> None:
        expired = [
            key
            for key, pending in self._pending.items()
            if pending.expires_at <= now
        ]
        for key in expired:
            del self._pending[key]
