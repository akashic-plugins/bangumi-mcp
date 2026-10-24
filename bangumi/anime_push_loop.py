"""放送提醒的后台刷新循环。

V3 目前没有外部插件可用的公开主动信息源能力，所以本模块只负责把
``AnimeUpdateCoordinator`` 从 MCP lifespan 搬到插件自己的生命周期：在
``RUNTIME_STARTED`` 启动、``RUNTIME_STOPPING`` 收束，用宿主 ``core.timers``
做一次性等待，不自己维护第二套调度协议。到期事件只写入本地库，不做自动投递。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ._host_contract import TimerHandle
from .anime_updates import AnimeUpdateCoordinator

KINDS = ("catalog", "schedule", "due")


class PluginTimersPort(Protocol):
    """宿主 ``core.timers`` 的窄读取面。"""

    def schedule(self, deadline: datetime) -> TimerHandle: ...


@dataclass
class AnimeUpdateLoop:
    """用宿主 timer 驱动协调器的三轮询循环。"""

    coordinator: AnimeUpdateCoordinator
    timers: PluginTimersPort | None = None
    # 每轮 due 之后把本地 pending 事件同步进告警邮箱；没有桥时为空。
    alert_sync: Callable[[], Awaitable[object]] | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _handle: TimerHandle | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """在当前 Fiber 内启动唯一一个刷新任务。"""

        if self._closed:
            raise RuntimeError("放送刷新循环已关闭")
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """取消定时等待并等待刷新任务退出。"""

        self._closed = True
        handle = self._handle
        task = self._task
        self._handle = None
        self._task = None
        if handle is not None:
            await handle.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadlines = {
            kind: loop.time() + self.coordinator.next_poll_delay(kind)
            for kind in KINDS
        }
        while not self._closed:
            kind = min(deadlines, key=lambda item: deadlines[item])
            delay = deadlines[kind] - loop.time()
            if delay > 0:
                await self._wait(delay)
                continue
            delay = await self.coordinator.run_step(kind)
            deadlines[kind] = loop.time() + delay
            if kind == "due" and self.alert_sync is not None:
                # alert_sync 是协程（v2 告警桥是 async），不能放进线程池。
                await self.alert_sync()

    async def _wait(self, delay: float) -> None:
        if self.timers is None:
            raise RuntimeError("放送刷新循环缺少 core.timers")
        deadline = datetime.now(timezone.utc) + timedelta(seconds=delay)
        handle = self.timers.schedule(deadline)
        self._handle = handle
        try:
            _ = await handle.result()
        finally:
            self._handle = None
            await handle.cleanup()


__all__ = ["AnimeUpdateLoop", "PluginTimersPort"]
