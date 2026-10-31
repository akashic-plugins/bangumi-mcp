from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from bangumi_test_plugin import plugin
from bangumi_test_plugin.bangumi._host_contract import (
    CREDENTIALS,
    INSTALLED_ASSETS,
    TIMERS,
    TOOLS,
    CredentialRef,
)
from bangumi_test_plugin.bangumi.client import BangumiClient

from tests._catalog import discovery_catalog


@dataclass
class _DeclareGroupCall:
    always_on: bool
    description: str


@dataclass
class _RegisterCall:
    name: str
    description: str
    parameters: Mapping[str, object]
    idempotent: bool
    parallel: bool
    open: Any


class _ToolServiceRecorder:
    """记录 apply 的工具注册调用，并允许触发一次真实 binding open。"""

    def __init__(self) -> None:
        self.declare_groups: list[_DeclareGroupCall] = []
        self.registered: list[_RegisterCall] = []

    async def declare_group(
        self,
        ctx: object,
        *,
        always_on: bool = False,
        description: str = "未声明用途",
    ) -> object:
        del ctx
        self.declare_groups.append(
            _DeclareGroupCall(always_on=always_on, description=description)
        )
        return object()

    async def register(
        self,
        ctx: object,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        open: Any,
        capture: object = None,
        public: bool = True,
        idempotent: bool = False,
        parallel: bool = False,
        exclusive_batch: bool = False,
        release: object = None,
    ) -> object:
        del ctx, capture, public, exclusive_batch, release
        self.registered.append(
            _RegisterCall(
                name=name,
                description=description,
                parameters=parameters,
                idempotent=idempotent,
                parallel=parallel,
                open=open,
            )
        )
        return object()

    def view(self, *refs: object) -> object:
        return refs


class _AssetServiceRecorder:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    def __call__(self) -> tuple[object, ...]:
        return ()

    async def register(
        self,
        ctx: object,
        category: str,
        relative_path: str,
    ) -> object:
        del ctx
        self.registered.append((category, relative_path))
        return object()


class _Lease:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values
        self.closed = False

    def credential(self, ref: CredentialRef) -> str:
        # 与宿主 CoreProviderClient 一致：关闭后再读凭据必须报错。
        if self.closed:
            raise RuntimeError("provider credential client 已关闭")
        return self._values[ref.path[0]]

    async def aclose(self) -> None:
        self._values.clear()
        self.closed = True


class _CredentialServiceRecorder:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values
        self.opened: list[Mapping[str, CredentialRef]] = []
        self.leases: list[_Lease] = []

    def open(
        self,
        ctx: object,
        refs: Mapping[str, CredentialRef],
    ) -> Any:
        del ctx
        self.opened.append(dict(refs))
        lease = _Lease(self._values)
        self.leases.append(lease)

        @asynccontextmanager
        async def manager() -> AsyncIterator[_Lease]:
            # 与宿主 CredentialClients.open 一致：退出上下文时才关闭 client。
            try:
                yield lease
            finally:
                await lease.aclose()

        return manager()


class _FakeTimerHandle:
    def __init__(self) -> None:
        self.cancelled = False

    async def result(self) -> object:
        raise RuntimeError("测试不会等待真实定时器")

    async def cancel(self) -> object:
        self.cancelled = True
        return object()

    async def cleanup(self) -> None:
        self.cancelled = True


class _FakeTimers:
    def __init__(self) -> None:
        self.deadlines: list[object] = []

    def schedule(self, deadline: object) -> _FakeTimerHandle:
        self.deadlines.append(deadline)
        return _FakeTimerHandle()


@dataclass
class _FakeContext:
    config: Mapping[str, object]
    data_root: Path
    services: dict[object, object] = field(default_factory=dict)
    listeners: list[tuple[object, object]] = field(default_factory=list)
    optional: list[tuple[tuple[object, ...], object, str]] = field(
        default_factory=list
    )

    def require(self, key: object) -> object:
        try:
            return self.services[key]
        except KeyError as error:
            raise AssertionError(f"apply 请求了未提供的 service: {key!r}") from error

    async def on(self, key: object, listener: object) -> object:
        self.listeners.append((key, listener))
        return object()

    async def inject(
        self,
        dependencies: tuple[object, ...],
        apply: object,
        *,
        name: str | None = None,
    ) -> object:
        self.optional.append((dependencies, apply, name or ""))
        return object()


_CONFIG = {
    "access_token": CredentialRef(("bangumi-token", "rev")),
    "user_agent": "test-agent/1.0",
}


def _stub_api(path: str) -> object:
    if path == "/v0/me":
        return {"username": "tester"}
    if path.startswith("/v0/users/"):
        return {
            "total": 1,
            "limit": 10,
            "offset": 0,
            "data": [
                {
                    "subject_id": 42,
                    "subject_type": 2,
                    "rate": 8,
                    "type": 3,
                    "ep_status": 5,
                    "vol_status": 0,
                    "updated_at": "2026-08-03T12:00:00+08:00",
                    "private": False,
                    "subject": {
                        "id": 42,
                        "type": 2,
                        "name": "Test Anime",
                        "name_cn": "测试动画",
                        "eps": 12,
                        "volumes": 0,
                    },
                }
            ],
        }
    raise AssertionError(f"未预期的请求: {path}")


def _run_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: Mapping[str, object] | None = None,
) -> tuple[_ToolServiceRecorder, _AssetServiceRecorder, _CredentialServiceRecorder]:
    tools = _ToolServiceRecorder()
    assets = _AssetServiceRecorder()
    credentials = _CredentialServiceRecorder({"bangumi-token": "secret-token"})
    ctx = _FakeContext(
        config=config if config is not None else _CONFIG,
        data_root=tmp_path,
        services={
            TOOLS: tools,
            INSTALLED_ASSETS: assets,
            CREDENTIALS: credentials,
            TIMERS: _FakeTimers(),
        },
    )
    monkeypatch.setattr(
        BangumiClient,
        "_request_json",
        lambda self, method, path, **kwargs: _stub_api(path),
    )
    asyncio.run(plugin.apply(ctx))  # type: ignore[arg-type]
    return tools, assets, credentials


def test_apply_registers_skills_and_every_catalog_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, assets, _ = _run_apply(tmp_path, monkeypatch)
    expected = discovery_catalog()

    assert assets.registered == [("skills", "skills")]
    assert len(tools.declare_groups) == 1
    assert tools.declare_groups[0].always_on is True
    assert tools.declare_groups[0].description == plugin.desc
    assert [call.name for call in tools.registered] == list(expected.names)
    for call in tools.registered:
        entry = expected[call.name]
        assert call.parameters == entry.parameters
        assert call.description == entry.description
        assert call.idempotent is entry.idempotent
        assert call.parallel is entry.parallel


def test_registered_tool_invocation_reads_credential_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _, credentials = _run_apply(tmp_path, monkeypatch)
    call = next(
        item for item in tools.registered if item.name == "list_collections"
    )

    async def exercise() -> object:
        async with call.open({}) as bound:
            return await bound.invoke("key", {"subject_type": "anime"})

    result = asyncio.run(exercise())

    assert result.outcome == "success"
    assert '"total":1' in result.parts[0].value
    assert credentials.opened == [
        {"access_token": CredentialRef(("bangumi-token", "rev"))}
    ]
    assert credentials.leases[0].closed is True


def test_tool_validation_error_returns_error_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _, _ = _run_apply(tmp_path, monkeypatch)
    call = next(
        item for item in tools.registered if item.name == "get_collection_status"
    )

    async def exercise() -> object:
        async with call.open({}) as bound:
            return await bound.invoke("key", {})

    result = asyncio.run(exercise())

    assert result.outcome == "error"
    assert "subject_id" in result.parts[0].value


def test_bound_tool_exposes_identity_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _, _ = _run_apply(tmp_path, monkeypatch)
    call = next(
        item for item in tools.registered if item.name == "get_collection_status"
    )

    async def exercise() -> None:
        async with call.open({}) as bound:
            assert bound.idempotent is True
            assert await bound.prepare({"subject_id": 1}) == {"subject_id": 1}
            assert await bound.query("key") is None

    asyncio.run(exercise())
