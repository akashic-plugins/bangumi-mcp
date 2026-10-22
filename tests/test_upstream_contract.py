"""对真实宿主的契约校验：加载、key 身份、结果值类型、告警桥 lifecycle。

与 `test_target_contract.py` 的分工：

- `test_target_contract.py` 只检查**本仓库声明的形状**，任何环境都能跑；
- 本模块把形状放进**真实宿主**里验证，包括用宿主自己的加载器导入 `plugin.py`、
  用宿主的 `ServiceKey` 比较 key 身份、用宿主的 `coerce_result` 校验工具结果。

没有宿主源码时整模块跳过（见 `tests/_host.py`），不会让插件仓库的 CI 判红。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from tests._host import import_host, requires_host

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin.py"

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

pytestmark = requires_host


def _host_module(name: str) -> object:
    return import_host(name)


# ---------------------------------------------------------------------------
# 加载：宿主 loader 以 `_akashic_input_<hex>` 身份 exec plugin.py
# ---------------------------------------------------------------------------


_LOADED: dict[str, object] = {}


def _load_plugin_as_host_module(module_name: str = "_akashic_input_contract") -> object:
    """用宿主自己的 FreshPluginImporter 加载，复刻 manager._import_plugin 的行为。"""

    if module_name in _LOADED:
        return _LOADED[module_name]
    importer_module = import_host("agent.plugins.importer")
    importer = importer_module.FreshPluginImporter()
    importer.register(module_name, REPO)
    spec = importer.root_spec(module_name, PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        importer.unregister(module_name)
    _LOADED[module_name] = module
    return module


def test_plugin_imports_under_the_host_loader() -> None:
    """文档 3.1：宿主不把插件根加进 sys.path，绝对自导入会 ModuleNotFoundError。"""

    module = _load_plugin_as_host_module()

    assert module.api_version == 3
    assert module.name == "bangumi"
    assert callable(module.apply)


def test_plugin_inject_uses_keys_the_host_actually_provides() -> None:
    """文档 3.2 / 4.2：key 身份就是 name 字符串，必须与提供方的 ServiceKey 相等。"""

    module = _load_plugin_as_host_module()
    composition = import_host("agent.plugin_composition")
    tools_contract = import_host("plugins.tools.contract")
    assets_contract = import_host("plugins.assets.contract")
    timer_contract = import_host("plugins.timer.contract")

    host_keys = {
        "tools.v1": tools_contract.TOOLS,
        "assets.installed.v1": assets_contract.INSTALLED_ASSETS,
        "core.credentials": composition.CREDENTIALS,
        "timers.v1": timer_contract.TIMERS,
    }
    injected = {key.name: key for key in module.__dict__["inject"]}  # type: ignore[index]

    unknown = set(injected) - set(host_keys)
    assert unknown == set(), f"inject 里有宿主不提供的 key: {unknown}"
    for name, host_key in host_keys.items():
        if name in injected:
            assert injected[name] == host_key, f"{name} 与宿主 ServiceKey 不相等"


def test_optional_eventmail_keys_match_the_provider() -> None:
    """文档 4.2 / 4.5：eventmail v2 的两个可选 key 必须与提供方一致。"""

    contract = import_host("plugins.eventmail.contract")
    from bangumi import _host_contract as local

    assert local.ALERT_SOURCE == contract.EVENTMAIL_ALERT_SOURCE
    assert local.CONTEXT_SOURCE == contract.EVENTMAIL_CONTEXT_SOURCE


# ---------------------------------------------------------------------------
# 工具结果：宿主执行边界的真实校验
# ---------------------------------------------------------------------------


def test_host_accepts_our_tool_results() -> None:
    """文档 4.3 + 3.4：`coerce_result` 会重新构造 Result 并逐块 isinstance 校验。"""

    api = import_host("plugins.tools.api")
    from bangumi_test_plugin.bangumi.tools import tool_error, tool_success

    accepted = api.coerce_result(tool_success({"total": 2}))
    rejected = api.coerce_result(tool_error("boom"))

    assert accepted.outcome == "success"
    assert '"total":2' in cast(str, accepted.parts[0].value)
    assert rejected.outcome == "error"

    ledger = import_host("plugins.ledger.contract")
    for part in (*accepted.parts, *rejected.parts):
        assert isinstance(part, ledger.ContentPart)


def test_host_rejects_the_old_stub_result() -> None:
    """回归保护：结构模仿的旧 `_TextPart` 必须仍被宿主拒绝。

    如果哪天宿主放宽到接受鸭子类型，这个测试会失败，提示我们可以简化实现。
    """

    from dataclasses import dataclass

    api = import_host("plugins.tools.api")

    @dataclass(frozen=True)
    class _LegacyTextPart:
        text: str
        type: str = "text"

    @dataclass(frozen=True)
    class _LegacyResult:
        outcome: str
        parts: tuple[object, ...]

    with pytest.raises(TypeError):
        api.coerce_result(_LegacyResult("success", (_LegacyTextPart("hi"),)))


# ---------------------------------------------------------------------------
# 工具注册：宿主的 register 必须接受我们传的全部关键字
# ---------------------------------------------------------------------------


def test_host_register_accepts_every_keyword_the_plugin_passes() -> None:
    """文档 3.3 / 4.4：宿主删了 risk，我们不能再传，也不能少传必需项。"""

    import inspect

    contract = import_host("plugins.tools.contract")
    parameters = set(inspect.signature(contract.ToolCatalog.register).parameters)

    assert "risk" not in parameters
    assert "search_hint" not in parameters
    for name in ("name", "description", "parameters", "open", "idempotent", "parallel"):
        assert name in parameters, f"宿主 register 不再接受 {name}"


def test_declared_tools_can_be_registered_against_the_host_validator() -> None:
    """文档 4.4：工具名 / schema / 描述必须过宿主的注册校验规则。"""

    import re

    from tests._catalog import discovery_catalog

    catalog = discovery_catalog()
    for entry in catalog.entries:
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", entry.name), entry.name
        assert entry.description.strip(), entry.name
        assert entry.parameters.get("type") == "object", entry.name


# ---------------------------------------------------------------------------
# eventmail v2：async + close 的真实形状
# ---------------------------------------------------------------------------


def test_host_eventmail_source_is_async_and_closable() -> None:
    """文档 4.5：v2 的 report/status 是 async，且 binding 有 close()。"""

    import inspect

    contract = import_host("plugins.eventmail.contract")

    assert inspect.iscoroutinefunction(contract.BoundAlertSource.report)
    assert inspect.iscoroutinefunction(contract.BoundAlertSource.status)
    assert inspect.iscoroutinefunction(contract.BoundContextSource.report)
    assert hasattr(contract.BoundAlertSource, "close")
    assert hasattr(contract.BoundContextSource, "close")
