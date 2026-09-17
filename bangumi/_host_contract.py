"""Bangumi 插件使用的宿主结构合同。

本模块只描述本插件实际依赖的窄接口，不 import 宿主实现，也不 import 兄弟插件。
运行时 ServiceKey 由同名 key 的动态绑定识别，因此这里声明的 key 名必须与宿主一致：

- ``tools.v1``：``plugins.tools`` 提供的工具目录 owner。
- ``core.installed_assets.v1``：``assets`` provider 提供的固定代码资产注册表。

只有宿主 SDK 不可用时才回退到最小 key 容器；离线测试用这个回退加载 plugin.py，
不需要伪造 `agent` 包。真实运行时始终走宿主的 ``ServiceKey``。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Generic, Literal, Protocol, TypeVar

try:  # 宿主运行时提供。
    from agent.plugin_composition import Context, Effect, ServiceKey
except ModuleNotFoundError:  # pragma: no cover - 只在没有宿主 SDK 的离线环境触发
    _T = TypeVar("_T")

    class ServiceKey(Generic[_T]):  # type: ignore[no-redef]
        """离线回退：只保留宿主真实 key 的字符串身份。"""

        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

        def __repr__(self) -> str:
            return f"ServiceKey({self.name!r})"

        def __eq__(self, other: object) -> bool:
            return isinstance(other, ServiceKey) and other.name == self.name

        def __hash__(self) -> int:
            return hash(self.name)

    class Context(Protocol):  # type: ignore[no-redef]
        """离线 fallback：只用于类型标注，不做静态检查。"""

    class Effect(Protocol):  # type: ignore[no-redef]
        """离线 fallback：只用于类型标注，不做静态检查。"""


TOOLS_SERVICE_NAME = "tools.v1"
INSTALLED_ASSETS_SERVICE_NAME = "core.installed_assets.v1"

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ContentPart(Protocol):
    """工具结果正文片段；宿主按 ``type`` 解释。"""

    @property
    def type(self) -> str: ...

    @property
    def text(self) -> str: ...


class ToolResult(Protocol):
    """工具 owner 接受的最小结果结构。"""

    @property
    def outcome(self) -> Literal["success", "denied", "error", "interrupted"]: ...

    @property
    def parts(self) -> tuple[ContentPart, ...]: ...


class CallSource(Protocol):
    """已提交调用的只读来源；工具不取得任意 MessageLog。"""

    @property
    def call_ref(self) -> object: ...

    @property
    def messages(self) -> tuple[object, ...]: ...


class BoundTool(Protocol):
    """一次工具 binding 的窄执行接口。"""

    @property
    def idempotent(self) -> bool: ...

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: CallSource | None = None,
    ) -> Mapping[str, object] | str: ...

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> ToolResult: ...

    async def query(self, key: str) -> ToolResult | None: ...


class ToolRef(Protocol):
    """工具 owner 发布的不可变发现引用。"""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> Mapping[str, object]: ...


class ToolView(Protocol):
    """已授予的一组工具引用。"""

    @property
    def refs(self) -> tuple[ToolRef, ...]: ...


OpenTool = Callable[[Mapping[str, object]], AbstractAsyncContextManager[BoundTool]]
Capture = Callable[[Mapping[str, object]], Mapping[str, object]]


class ToolCatalogService(Protocol):
    """``tools.v1`` 的注册与 binding 入口。"""

    async def declare_group(
        self,
        ctx: Context,
        *,
        always_on: bool = False,
        description: str = "未声明用途",
    ) -> Effect: ...

    async def register(
        self,
        ctx: Context,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        open: OpenTool,
        capture: Capture | None = None,
        public: bool = True,
        idempotent: bool = False,
        risk: Literal[
            "read-only",
            "read-write",
            "external-side-effect",
        ] = "read-write",
        search_hint: str | None = None,
    ) -> ToolRef: ...

    def view(self, *refs: ToolRef) -> ToolView: ...


class InstalledAsset(Protocol):
    """一个已登记的固定代码资产集合。"""

    @property
    def owner_id(self) -> str: ...

    @property
    def category(self) -> str: ...

    @property
    def root_dir(self) -> object: ...


class InstalledAssets(Protocol):
    """``core.installed_assets.v1`` 的只读目录与登记入口。"""

    def __call__(self) -> tuple[InstalledAsset, ...]: ...

    async def register(
        self,
        ctx: Context,
        category: str,
        relative_path: str,
    ) -> Effect: ...


TOOLS = ServiceKey[ToolCatalogService](TOOLS_SERVICE_NAME)
INSTALLED_ASSETS = ServiceKey[InstalledAssets](INSTALLED_ASSETS_SERVICE_NAME)


def tool_name_error(name: object) -> str | None:
    """返回工具名不合法的原因，合法时返回 ``None``。"""

    if not isinstance(name, str):
        return "工具名必须是字符串"
    if _TOOL_NAME.fullmatch(name) is None:
        return "工具名必须匹配 ^[a-z][a-z0-9_]{0,63}$"
    return None


__all__ = [
    "INSTALLED_ASSETS",
    "INSTALLED_ASSETS_SERVICE_NAME",
    "TOOLS",
    "TOOLS_SERVICE_NAME",
    "BoundTool",
    "CallSource",
    "Capture",
    "ContentPart",
    "Context",
    "Effect",
    "InstalledAsset",
    "InstalledAssets",
    "OpenTool",
    "ServiceKey",
    "ToolCatalogService",
    "ToolRef",
    "ToolResult",
    "ToolView",
    "tool_name_error",
]
