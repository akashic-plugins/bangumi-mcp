from __future__ import annotations

from bangumi_test_plugin.bangumi._host_contract import ToolResult
from bangumi_test_plugin.bangumi.tools import (
    ToolCatalog,
    ToolExecutionError,
    build_tool_catalog,
)


async def _unwired(method: str, *args: object, **kwargs: object) -> ToolResult:
    del args, kwargs
    raise ToolExecutionError(f"发现合同测试不应调用领域方法: {method}")


def discovery_catalog() -> ToolCatalog:
    """构造只用于校验发现合同的目录；领域调用会明确失败。"""

    return build_tool_catalog(invoke=_unwired)
