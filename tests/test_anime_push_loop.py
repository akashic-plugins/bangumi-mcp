from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import pytest
from bangumi_test_plugin import plugin
from bangumi_test_plugin.bangumi._host_contract import (
    CREDENTIALS,
    INSTALLED_ASSETS,
    TIMERS,
    TOOLS,
    CredentialRef,
)
from bangumi_test_plugin.bangumi.anime_push_loop import AnimeUpdateLoop
from bangumi_test_plugin.bangumi.anime_updates import (
    AnimeUpdateCoordinator,
    AnimeUpdateStore,
)

from tests.test_apply_registration import (
    _CONFIG,
    _AssetServiceRecorder,
    _CredentialServiceRecorder,
    _FakeContext,
    _FakeTimers,
    _ToolServiceRecorder,
)


def _record_sync(syncs: list[int]) -> Callable[[], Awaitable[None]]:
    """返回一个 async 回调：v2 告警桥是 async，循环必须 await 它而不是丢进线程池。"""

    async def sync() -> None:
        syncs.append(1)

    return sync


class _BlockingTimerHandle:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._future: asyncio.Future[None] = loop.create_future()
        self.cancelled = False
        self.cleaned = False

    async def result(self) -> object:
        await self._future
        return object()

    async def cancel(self) -> object:
        self.cancelled = True
        if not self._future.done():
            self._future.cancel()
        return object()

    async def cleanup(self) -> None:
        self.cleaned = True


class _BlockingTimers:
    def __init__(self) -> None:
        self.handles: list[_BlockingTimerHandle] = []

    def schedule(self, deadline: object) -> _BlockingTimerHandle:
        del deadline
        handle = _BlockingTimerHandle(asyncio.get_running_loop())
        self.handles.append(handle)
        return handle


def _stub_api(path: str) -> object:
    if path == "/v0/me":
        return {"username": "tester"}
    return {"total": 0, "limit": 50, "offset": 0, "data": []}


class _FakeCoordinator:
    def __init__(self, delays: Mapping[str, float] | None = None) -> None:
        self._delays = dict(delays or {})
        self.calls: list[str] = []

    def next_poll_delay(self, kind: str) -> float:
        return self._delays.get(kind, 0.0)

    async def run_step(self, kind: str) -> float:
        self.calls.append(kind)
        return 60.0


def test_loop_runs_alert_sync_after_each_due_step() -> None:
    coordinator = _FakeCoordinator({"catalog": 3600.0, "schedule": 3600.0, "due": 0.0})
    syncs: list[int] = []
    loop = AnimeUpdateLoop(
        coordinator=coordinator,  # type: ignore[arg-type]
        timers=_BlockingTimers(),  # type: ignore[arg-type]
        alert_sync=_record_sync(syncs),
    )

    async def exercise() -> None:
        await loop.start()
        for _ in range(50):
            await asyncio.sleep(0)
            if syncs:
                break
        await loop.stop()

    asyncio.run(exercise())

    assert coordinator.calls == ["due"]
    assert syncs, "due 轮询后必须同步一次告警邮箱"


def test_loop_skips_alert_sync_without_bridge() -> None:
    coordinator = _FakeCoordinator({"catalog": 3600.0, "schedule": 3600.0, "due": 0.0})
    loop = AnimeUpdateLoop(
        coordinator=coordinator,  # type: ignore[arg-type]
        timers=_BlockingTimers(),  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        await loop.start()
        for _ in range(30):
            await asyncio.sleep(0)
        await loop.stop()

    asyncio.run(exercise())

    assert coordinator.calls == ["due"]


def test_loop_runs_steps_and_schedules_timer_waits() -> None:
    coordinator = _FakeCoordinator({"catalog": 0.0, "schedule": 3600.0, "due": 0.0})
    timers = _BlockingTimers()
    loop = AnimeUpdateLoop(coordinator=coordinator, timers=timers)  # type: ignore[arg-type]

    async def exercise() -> None:
        await loop.start()
        for _ in range(50):
            await asyncio.sleep(0)
            if coordinator.calls.count("catalog") >= 1 and timers.handles:
                break
        await loop.stop()

    asyncio.run(exercise())

    assert "catalog" in coordinator.calls
    assert "due" in coordinator.calls
    assert timers.handles, "loop 必须用 timers.v1 的一次等待而不是自己 sleep"
    assert timers.handles[0].cancelled is True


def test_loop_stop_is_idempotent() -> None:
    coordinator = _FakeCoordinator({"catalog": 60.0, "schedule": 60.0, "due": 60.0})
    loop = AnimeUpdateLoop(coordinator=coordinator, timers=_BlockingTimers())  # type: ignore[arg-type]

    async def exercise() -> None:
        await loop.start()
        await loop.stop()
        await loop.stop()

    asyncio.run(exercise())

    assert coordinator.calls == []


def test_loop_refuses_to_start_without_timers() -> None:
    loop = AnimeUpdateLoop(coordinator=_FakeCoordinator())  # type: ignore[arg-type]

    async def exercise() -> None:
        await loop.start()
        for _ in range(10):
            await asyncio.sleep(0)
        await loop.stop()

    asyncio.run(exercise())


def test_coordinator_exposes_public_step_api(tmp_path: Path) -> None:
    store = AnimeUpdateStore(tmp_path / "anime_updates.db")

    class _Bangumi:
        def get_me(self) -> dict[str, object]:
            return {"username": "tester"}

        def list_collections(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"total": 0, "limit": 50, "offset": 0, "data": []}

    class _AniList:
        pass

    from bangumi_test_plugin.bangumi.settings import AnimePushSettings

    coordinator = AnimeUpdateCoordinator(
        store,
        _Bangumi(),  # type: ignore[arg-type]
        _AniList(),  # type: ignore[arg-type]
        AnimePushSettings(enabled=True),
    )

    assert coordinator.next_poll_delay("catalog") == 0.0
    delay = asyncio.run(coordinator.run_step("catalog"))
    assert delay > 0
    assert coordinator.next_poll_delay("catalog") > 0

    with pytest.raises(ValueError, match="未知轮询类型"):
        asyncio.run(coordinator.run_step("nope"))


def _run_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: Mapping[str, object],
) -> tuple[_FakeContext, _CredentialServiceRecorder]:
    tools = _ToolServiceRecorder()
    assets = _AssetServiceRecorder()
    credentials = _CredentialServiceRecorder(
        {"bangumi-token": "secret", "anilist-token": "anilist"}
    )
    timers = _FakeTimers()
    ctx = _FakeContext(
        config=config,
        data_root=tmp_path,
        services={
            TOOLS: tools,
            INSTALLED_ASSETS: assets,
            CREDENTIALS: credentials,
            TIMERS: timers,
        },
    )
    monkeypatch.setattr(
        "bangumi.client.BangumiClient._request_json",
        lambda self, method, path, **kwargs: _stub_api(path),
    )
    asyncio.run(plugin.apply(ctx))  # type: ignore[arg-type]
    return ctx, credentials


def test_apply_registers_refresh_lifecycle_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled, _ = _run_apply(tmp_path, monkeypatch, config=_CONFIG)

    assert disabled.listeners == []
    # 桥在两边都注册：已有 pending 事件仍应能被投递。
    assert [name for _, _, name in disabled.optional] == ["bangumi-alert-bridge"]

    enabled, _ = _run_apply(
        tmp_path,
        monkeypatch,
        config={
            "access_token": _CONFIG["access_token"],
            "user_agent": "test-agent/1.0",
            "anime_push": {
                "enabled": True,
                "anilist_token": CredentialRef(("anilist-token", "rev")),
            },
        },
    )

    names = [getattr(key, "name", "") for key, _ in enabled.listeners]
    assert names == ["runtime.started", "runtime.stopping"]

    # 告警桥是可选依赖：eventmail 缺席时降级为纯本地抓取。
    assert [name for _, _, name in enabled.optional] == ["bangumi-alert-bridge"]
    dependencies, _, _ = enabled.optional[0]
    assert [getattr(key, "name", "") for key in dependencies] == [
        "eventmail.alert_source.v2",
        "eventmail.context_source.v2",
    ]


def test_lifecycle_start_and_stop_open_and_close_background_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, credentials = _run_apply(
        tmp_path,
        monkeypatch,
        config={
            "access_token": _CONFIG["access_token"],
            "anime_push": {
                "enabled": True,
                "anilist_token": CredentialRef(("anilist-token", "rev")),
            },
        },
    )
    listeners = {
        getattr(key, "name", ""): listener for key, listener in ctx.listeners
    }
    started = listeners["runtime.started"]
    stopping = listeners["runtime.stopping"]

    # 用纯假协调器替换真实实现，只验证生命周期与租约接线。
    from bangumi import runtime as runtime_module

    coordinator = _FakeCoordinator(
        {"catalog": 3600.0, "schedule": 3600.0, "due": 3600.0}
    )
    monkeypatch.setattr(
        runtime_module.BangumiRuntime,
        "_coordinator",
        lambda self: coordinator,
    )

    async def exercise() -> None:
        await started(object())
        for _ in range(20):
            await asyncio.sleep(0)
            if credentials.opened:
                break
        await stopping(object())

    asyncio.run(exercise())

    assert credentials.opened, "后台刷新必须开自己的凭据租约"
    assert credentials.leases[-1].closed is True
