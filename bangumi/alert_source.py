"""放送事件到 eventmail 的投递桥。

V3 里外部插件不能自己推送消息，但第一方 ``eventmail`` 插件提供了公开的告警
邮箱，``wake`` 插件会以 ``owner="alert"`` 流程消费它并投递用户可见消息。

关键约束（已核实）：Wake 的 alert 阶段只把 ``Request.alert_ref``（三个标识）
放进上下文，**告警 payload 本身不渲染**；模型能看到的是
``active_context()`` 返回的 ``context_events``，其 payload 原样进提示词。
所以本桥对同一个 ``event_id`` 同时上报：

- ``Alert``：只当“到点了”的触发器，驱动 wake 启动 alert 流程；
- ``Context``：承载标题、集数、计划放送时刻等事实，供模型写正文。

本地 ``anime_updates.db`` 仍是插件自己的真相源；邮箱只负责投递。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ._host_contract import BoundAlertSource, BoundContextSource

SOURCE_ID = "bangumi"

# 投递终态：见到这些状态就可以从本地 pending 队列确认掉。
TERMINAL_STATUSES = frozenset({"delivered", "skipped", "expired"})

# 上下文在告警后保留的窗口；到期后 active_context 自行忽略，无需手动清理。
CONTEXT_TTL = timedelta(hours=24)


@dataclass
class AlertBinding:
    """一次绑定：告警触发器与上下文材料指向同一个来源与事件。"""

    alerts: BoundAlertSource
    contexts: BoundContextSource
    source_id: str = SOURCE_ID

    def report(
        self,
        payload: Mapping[str, object],
        *,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> None:
        event_id = _event_id(payload)
        context = _context_payload(payload)
        _ = self.contexts.report(
            event_id=event_id,
            payload=context,
            observed_at=now,
            expires_at=now + CONTEXT_TTL,
        )
        _ = self.alerts.report(
            event_id=event_id,
            payload=context,
            observed_at=now,
            expires_at=expires_at,
        )

    def is_terminal(self, payload: Mapping[str, object]) -> bool:
        status = self.alerts.status(event_id=_event_id(payload))
        return status in TERMINAL_STATUSES


@dataclass
class BangumiAlertBridge:
    """把本地 pending 事件同步进 eventmail，并确认已投递的事件。"""

    store: object
    binding: AlertBinding
    batch_size: int = 50
    _reported: set[str] = field(default_factory=set, repr=False)

    def tick(self, now: datetime) -> dict[str, int]:
        """上报尚未上报的事件，确认已到终态的事件。"""

        pending = _pending(self.store, self.batch_size)
        reported = 0
        acknowledged = 0
        for payload in pending:
            event_id = _event_id(payload)
            if self.binding.is_terminal(payload):
                self._acknowledge(event_id, now)
                acknowledged += 1
                continue
            if event_id in self._reported:
                continue
            self.binding.report(payload, now=now)
            self._reported.add(event_id)
            reported += 1
        return {"reported": reported, "acknowledged": acknowledged}

    def _acknowledge(self, event_id: str, now: datetime) -> None:
        self._reported.discard(event_id)
        count = self.store.acknowledge([event_id], now)  # type: ignore[attr-defined]
        if count != 1:
            raise RuntimeError(f"放送事件确认失败: {event_id}")


def _pending(store: object, limit: int) -> Sequence[Mapping[str, object]]:
    rows = store.fetch_pending(0, limit)  # type: ignore[attr-defined]
    if not isinstance(rows, list):
        raise TypeError("放送事件库返回了非列表快照")
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("放送事件不是对象")
    return rows


def _event_id(payload: Mapping[str, object]) -> str:
    value = payload.get("event_id")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("放送事件缺少 event_id")
    return value.strip()


def _context_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """只保留可渲染给模型的事实字段，并补上稳定的来源身份。"""

    context: dict[str, object] = {"source_name": "Bangumi × AniList"}
    for key in (
        "event_id",
        "kind",
        "source_type",
        "title",
        "content",
        "severity",
        "scheduled_at",
    ):
        value = payload.get(key)
        if value is not None:
            context[key] = value
    return context


__all__ = [
    "CONTEXT_TTL",
    "SOURCE_ID",
    "TERMINAL_STATUSES",
    "AlertBinding",
    "BangumiAlertBridge",
]
