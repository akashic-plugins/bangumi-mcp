from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

import plugin
from bangumi._host_contract import INSTALLED_ASSETS, TOOLS
from bangumi.tools import TOOL_CATALOG, ToolExecutionError


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
    risk: str
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
        risk: str = "read-write",
        search_hint: str | None = None,
    ) -> object:
        del ctx, capture, public, search_hint
        self.registered.append(
            _RegisterCall(
                name=name,
                description=description,
                parameters=parameters,
                idempotent=idempotent,
                risk=risk,
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


@dataclass
class _FakeContext:
    services: dict[object, object] = field(default_factory=dict)

    def require(self, key: object) -> object:
        try:
            return self.services[key]
        except KeyError as error:
            raise AssertionError(f"apply 请求了未提供的 service: {key!r}") from error


def _run_apply() -> tuple[_ToolServiceRecorder, _AssetServiceRecorder]:
    tools = _ToolServiceRecorder()
    assets = _AssetServiceRecorder()
    ctx = _FakeContext(services={TOOLS: tools, INSTALLED_ASSETS: assets})
    asyncio.run(plugin.apply(ctx))  # type: ignore[arg-type]
    return tools, assets


def test_apply_registers_skills_and_every_catalog_tool() -> None:
    tools, assets = _run_apply()

    assert assets.registered == [("skills", "skills")]
    assert len(tools.declare_groups) == 1
    assert tools.declare_groups[0].always_on is True
    assert tools.declare_groups[0].description == plugin.desc
    assert [call.name for call in tools.registered] == list(TOOL_CATALOG.names)
    for call in tools.registered:
        entry = TOOL_CATALOG[call.name]
        assert call.parameters == entry.parameters
        assert call.description == entry.description
        assert call.idempotent is entry.idempotent
        assert call.risk == entry.risk


def test_each_registered_open_yields_a_working_binding() -> None:
    tools, _ = _run_apply()

    async def exercise() -> None:
        for call in tools.registered:
            async with call.open({}) as bound:
                assert bound.idempotent is call.idempotent
                assert await bound.prepare({"subject_id": 1}) == {"subject_id": 1}
                assert await bound.query("key") is None

    asyncio.run(exercise())


def test_phase0_bound_tool_fails_loudly_instead_of_faking_results() -> None:
    tools, _ = _run_apply()
    call = next(
        item for item in tools.registered if item.name == "count_collections"
    )

    async def exercise() -> None:
        async with call.open({}) as bound:
            with pytest.raises(ToolExecutionError):
                await bound.invoke("key", {})

    asyncio.run(exercise())
