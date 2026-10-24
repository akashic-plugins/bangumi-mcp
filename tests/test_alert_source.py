"""告警桥：eventmail v2 的两个来源端口。

v2 相对 v1 的三处变化决定了本文件的形状（见 docs/design/plugin-contract-refresh.md 4.5）：

- `report` / `status` 是 **async**，同步调用只会得到未 await 的协程；
- binding 新增 `close()`，必须显式释放 `source_id` 所有权；
- 同一 `source_id` 在释放前不能被二次绑定。

断言内容（双通道上报、payload 渲染、TTL、终态、去重、ACK、真实事件库）
与 v1 时代一致，替换的只是调用方式。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import pytest
from bangumi_test_plugin.bangumi.alert_source import (
    CONTEXT_TTL,
    SOURCE_ID,
    TERMINAL_STATUSES,
    AlertBinding,
    BangumiAlertBridge,
)
from bangumi_test_plugin.bangumi.anime_updates import AnimeUpdateStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

EVENT = {
    "event_id": "episode:1007",
    "kind": "alert",
    "source_type": "anime_schedule",
    "source_name": "Bangumi × AniList",
    "title": "《测试动画》第 7 集计划于 20:00 放送",
    "content": "计划放送时间：2026-09-16 20:00（Asia/Shanghai）。",
    "severity": "low",
    "scheduled_at": "2026-09-16T12:00:00Z",
}

T = TypeVar("T")


def run(awaitable: Awaitable[T]) -> T:
    """在没有运行中 loop 的测试里跑一次协程。"""

    return asyncio.run(awaitable)


class _BoundRecorder:
    """模拟 v2 binding：async report/status，并有 close() 释放所有权。"""

    def __init__(self, *, statuses: Mapping[str, str] | None = None) -> None:
        self.reports: list[dict[str, object]] = []
        self.statuses = dict(statuses or {})
        self.closed = False

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError("binding 已关闭")

    async def report(self, **kwargs: object) -> Mapping[str, object]:
        self._check_open()
        self.reports.append(dict(kwargs))
        return {"accepted": True}

    async def status(self, *, event_id: str) -> str | None:
        self._check_open()
        return self.statuses.get(event_id)

    def close(self) -> None:
        self.closed = True


class _SourceRecorder:
    """模拟 v2 来源服务：同一 source_id 只能绑定一次，释放后才可重绑。"""

    def __init__(self, bound: _BoundRecorder) -> None:
        self._bound = bound
        self.bound_ids: list[str] = []
        self._active: str | None = None

    def bind(self, source_id: str) -> _BoundRecorder:
        if self._active == source_id:
            raise RuntimeError(f"source_id 已有 owner: {source_id}")
        self.bound_ids.append(source_id)
        self._active = source_id
        return self._bound

    def release(self) -> None:
        self._active = None


class _Store:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.acked: list[list[str]] = []

    def fetch_pending(self, offset: int, limit: int) -> list[dict[str, object]]:
        del offset, limit
        return list(self.rows)

    def acknowledge(self, event_ids: list[str], now: datetime) -> int:
        del now
        self.acked.append(list(event_ids))
        self.rows = [
            row for row in self.rows if row["event_id"] not in set(event_ids)
        ]
        return len(event_ids)


def _binding(
    *,
    statuses: Mapping[str, str] | None = None,
) -> tuple[AlertBinding, _BoundRecorder, _BoundRecorder]:
    alerts = _BoundRecorder(statuses=statuses)
    contexts = _BoundRecorder()
    return AlertBinding(alerts=alerts, contexts=contexts), alerts, contexts


# ---------------------------------------------------------------------------
# AlertBinding：双通道上报
# ---------------------------------------------------------------------------


def test_report_sends_both_alert_and_context_with_same_event_id() -> None:
    binding, alerts, contexts = _binding()

    run(binding.report(EVENT, now=NOW))

    assert len(alerts.reports) == 1
    assert len(contexts.reports) == 1
    assert alerts.reports[0]["event_id"] == "episode:1007"
    assert contexts.reports[0]["event_id"] == "episode:1007"


def test_context_receives_the_full_rendered_payload() -> None:
    binding, _, contexts = _binding()

    run(binding.report(EVENT, now=NOW))

    payload = contexts.reports[0]["payload"]
    assert isinstance(payload, Mapping)
    assert payload["title"] == EVENT["title"]
    assert payload["content"] == EVENT["content"]
    assert payload["scheduled_at"] == EVENT["scheduled_at"]
    assert payload["source_name"] == "Bangumi × AniList"


def test_context_expires_within_a_bounded_window() -> None:
    binding, _, contexts = _binding()

    run(binding.report(EVENT, now=NOW))

    assert contexts.reports[0]["observed_at"] == NOW
    assert contexts.reports[0]["expires_at"] == NOW + CONTEXT_TTL
    assert contexts.reports[0]["expires_at"] > contexts.reports[0]["observed_at"]


def test_terminal_status_is_recognised() -> None:
    binding, _, _ = _binding(statuses={"episode:1007": "delivered"})

    assert run(binding.is_terminal(EVENT)) is True

    pending_binding, _, _ = _binding(statuses={"episode:1007": "pending"})
    assert run(pending_binding.is_terminal(EVENT)) is False
    assert TERMINAL_STATUSES == {"delivered", "skipped", "expired"}


# ---------------------------------------------------------------------------
# BangumiAlertBridge：去重、ACK
# ---------------------------------------------------------------------------


def test_bridge_reports_once_and_acknowledges_terminals() -> None:
    store = _Store([dict(EVENT)])
    binding, alerts, contexts = _binding()
    bridge = BangumiAlertBridge(store=store, binding=binding)

    first = run(bridge.tick(NOW))
    second = run(bridge.tick(NOW))

    assert first == {"reported": 1, "acknowledged": 0}
    assert second == {"reported": 0, "acknowledged": 0}
    assert len(alerts.reports) == 1
    assert len(contexts.reports) == 1


def test_bridge_acknowledges_delivered_events_and_stops_reporting() -> None:
    store = _Store([dict(EVENT)])
    binding, alerts, _ = _binding(statuses={"episode:1007": "delivered"})
    bridge = BangumiAlertBridge(store=store, binding=binding)

    result = run(bridge.tick(NOW))

    assert result == {"reported": 0, "acknowledged": 1}
    assert store.acked == [["episode:1007"]]
    assert alerts.reports == []
    assert store.rows == []


def test_bridge_reports_new_events_after_previous_ack() -> None:
    first = dict(EVENT)
    second = {**EVENT, "event_id": "episode:1008"}
    store = _Store([first])
    binding, alerts, _ = _binding(statuses={"episode:1007": "delivered"})
    bridge = BangumiAlertBridge(store=store, binding=binding)

    _ = run(bridge.tick(NOW))
    store.rows = [second]
    result = run(bridge.tick(NOW))

    assert result == {"reported": 1, "acknowledged": 0}
    assert [report["event_id"] for report in alerts.reports] == ["episode:1008"]


def test_bridge_requires_event_id() -> None:
    store = _Store([{"title": "缺少 event_id"}])
    binding, _, _ = _binding()
    bridge = BangumiAlertBridge(store=store, binding=binding)

    with pytest.raises(RuntimeError, match="event_id"):
        run(bridge.tick(NOW))


def test_source_id_is_stable() -> None:
    assert SOURCE_ID == "bangumi"


# ---------------------------------------------------------------------------
# v2 新增：所有权释放
# ---------------------------------------------------------------------------


def test_binding_close_releases_both_sources() -> None:
    """v2 的 close() 必须同时释放 alert 与 context 两个 binding。"""

    binding, alerts, contexts = _binding()

    binding.close()

    assert alerts.closed is True
    assert contexts.closed is True


def test_closed_binding_rejects_further_reports() -> None:
    """释放后再 report 必须失败，而不是静默写进已释放的来源。"""

    binding, _, _ = _binding()
    binding.close()

    with pytest.raises(RuntimeError):
        run(binding.report(EVENT, now=NOW))


def test_source_id_can_be_rebound_after_release() -> None:
    """「不 close 会在换代时炸」的根因：释放前不可重绑，释放后可重绑。"""

    recorder = _BoundRecorder()
    source = _SourceRecorder(recorder)

    _ = source.bind(SOURCE_ID)
    with pytest.raises(RuntimeError, match="已有 owner"):
        source.bind(SOURCE_ID)

    source.release()
    _ = source.bind(SOURCE_ID)
    assert source.bound_ids == [SOURCE_ID, SOURCE_ID]


# ---------------------------------------------------------------------------
# 真实事件库
# ---------------------------------------------------------------------------


def test_bridge_works_against_the_real_store(tmp_path: Path) -> None:
    """用真实事件库跑一遍：真实 fetch_pending 与 acknowledge 语义。"""

    store = AnimeUpdateStore(tmp_path / "anime_updates.db")
    store.initialize()
    with store._transaction() as connection:  # 测试直接种一行
        connection.execute(
            """
            INSERT INTO subjects(
                subject_id, title, original_title, air_date, subject_format,
                episode_count, catalog_fingerprint, active, verified_at
            ) VALUES (42, '测试动画', 'Test Anime', '2026-09-01', 'TV', 12,
                      'catalog-fingerprint', 1, '2026-09-16T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO episodes(
                episode_id, subject_id, episode_number, title, air_date,
                verified_at
            ) VALUES (1007, 42, 7, '第 7 集', '2026-09-16',
                      '2026-09-16T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO schedules(
                episode_id, subject_id, media_id, episode_number, airing_at,
                notify_at, mapping_fingerprint, state, created_at, updated_at
            ) VALUES (1007, 42, 154587, 7, 1000, 1000, 'fingerprint',
                      'pending', '2026-09-16T00:00:00+00:00',
                      '2026-09-16T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO events(event_id, episode_id, payload_json, state, created_at)
            VALUES ('episode:1007', 1007, ?, 'pending',
                    '2026-09-16T00:00:00+00:00')
            """,
            ('{"event_id":"episode:1007","title":"t"}',),
        )

    binding, alerts, contexts = _binding()
    bridge = BangumiAlertBridge(store=store, binding=binding)

    result = run(bridge.tick(NOW))

    assert result["reported"] == 1
    assert [report["event_id"] for report in alerts.reports] == ["episode:1007"]
    assert [report["event_id"] for report in contexts.reports] == ["episode:1007"]
    assert [row["event_id"] for row in store.fetch_pending(0, 50)] == ["episode:1007"]

    terminal, _, _ = _binding(statuses={"episode:1007": "delivered"})
    acknowledged = run(BangumiAlertBridge(store=store, binding=terminal).tick(NOW))

    assert acknowledged == {"reported": 0, "acknowledged": 1}
    assert store.fetch_pending(0, 50) == []
