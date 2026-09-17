"""Bangumi 插件 V3 入口。

底座只读取本文件顶层的 ``name`` / ``version`` / ``api_version`` 三个字面量，
再调用唯一入口 ``apply(ctx)``。配置、凭据、工具与 Skill 的注册全部在
``apply`` 内通过宿主 ServiceKey 完成；本模块不导出任何 V2 类式插件协议，
也没有模块级配置导出。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from bangumi._host_contract import (
    INSTALLED_ASSETS,
    TOOLS,
    BoundTool,
    CallSource,
    Context,
    ToolResult,
)
from bangumi.tools import TOOL_CATALOG, ToolCatalog, ToolEntry, ToolExecutionError

api_version = 3
name = "bangumi"
version = "0.6.0"
desc = "查询 Bangumi 收藏与观看进度；写入需逐字确认"

inject = (TOOLS, INSTALLED_ASSETS)


async def apply(ctx: Context) -> None:
    """注册本插件的 Skill 资产与工具目录。"""

    await ctx.require(INSTALLED_ASSETS).register(ctx, "skills", "skills")
    await _register_tool_catalog(ctx, TOOL_CATALOG)


async def _register_tool_catalog(ctx: Context, catalog: ToolCatalog) -> None:
    """把一个静态目录注册成宿主工具；每个条目固定自己的发现合同。"""

    tools = ctx.require(TOOLS)
    _ = await tools.declare_group(ctx, always_on=True, description=desc)
    for entry in catalog.entries:
        _ = await tools.register(
            ctx,
            name=entry.name,
            description=entry.description,
            parameters=entry.parameters,
            open=_BoundBangumiTool(entry).open,
            idempotent=entry.idempotent,
            risk=entry.risk,
        )


class _BoundBangumiTool:
    """一次工具 binding 的窄实现；领域实现推迟到 Phase 2 接入。

    Phase 0 只冻结发现合同：工具可被宿主列出并校验 schema，执行会明确失败，
    不会静默返回伪造结果。
    """

    def __init__(self, entry: ToolEntry) -> None:
        self._entry = entry

    @property
    def idempotent(self) -> bool:
        return self._entry.idempotent

    @asynccontextmanager
    async def open(
        self,
        state: Mapping[str, object],
    ) -> AsyncIterator[BoundTool]:
        del state
        yield self

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: CallSource | None = None,
    ) -> Mapping[str, object]:
        del source
        return arguments

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> ToolResult:
        del key, arguments
        raise ToolExecutionError(
            f"工具 {self._entry.name} 的领域实现尚未接入（Phase 2）"
        )

    async def query(self, key: str) -> ToolResult | None:
        del key
        return None


__all__ = ["api_version", "apply", "desc", "inject", "name", "version"]
