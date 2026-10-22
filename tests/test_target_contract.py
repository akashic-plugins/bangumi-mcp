"""目标契约测试：Akashic 插件边界重构后的接口形状。

本模块测的是 `docs/design/plugin-contract-refresh.md` 第 4 节承诺的**目标状态**，
不是当前实现。规则：

- 断言的是「重构后必须成立」的事实，所以**在重构完成前会有失败**；
  失败清单就是待办清单，不需要单独维护两份。
- 不 import 宿主源码，只校验名字、签名形状与结果值类型；宿主的真实校验
  （`coerce_result`、组合图）在 CI 里用 `AKASHIC_AGENT_ROOT` 单独跑，见
  `tests/test_upstream_contract.py`。
- 每个测试的 docstring 写明它对应文档的哪一条，失败时能直接定位。
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import get_type_hints

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_assignments(tree: ast.Module, name: str) -> list[ast.expr]:
    """收集顶层对 name 的所有赋值表达式（不含函数内的）。"""

    values: list[ast.expr] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in statement.targets
            ):
                values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            values.append(statement.value)
    return values


def _imported_modules(tree: ast.Module) -> list[str]:
    """返回所有 `from X import ...` 与 `import X` 的模块名（含相对导入）。"""

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            modules.append(f"{prefix}{node.module or ''}")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


# ---------------------------------------------------------------------------
# 4.1 导入方式：插件内部不得出现绝对自导入
# ---------------------------------------------------------------------------


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in REPO.rglob("*.py")
        if not any(
            part in {".git", ".venv", ".venv-test", "__pycache__", ".ruff_cache"}
            for part in path.relative_to(REPO).parts
        )
    )


@pytest.mark.parametrize(
    "path",
    [path for path in _python_sources() if "tests" not in path.relative_to(REPO).parts],
    ids=lambda path: path.relative_to(REPO).as_posix(),
)
def test_runtime_modules_never_import_the_plugin_absolutely(path: Path) -> None:
    """文档 4.1 + 3.1：宿主把插件注册为 `_akashic_input_<hex>`，插件根不在 sys.path，
    所以任何 `from bangumi_test_plugin.bangumi.…` / `import bangumi` 都会 ModuleNotFoundError。

    tests/ 目录不受此限：测试以顶层模块身份运行，走 `__package__` 的另一条分支。
    """

    tree = _parse(path)
    absolute_self_imports = [
        module
        for module in _imported_modules(tree)
        if module == "bangumi" or module.startswith("bangumi.")
    ]
    assert absolute_self_imports == [], (
        f"{path.relative_to(REPO)} 存在绝对自导入 {absolute_self_imports}；"
        "宿主加载器下无法解析"
    )


def test_plugin_py_uses_only_relative_imports() -> None:
    """文档 4.1：`plugin.py` 只写 `from .bangumi.…`，不给测试加分支。

    对齐 `akashic-plugins/fitbit-mcp` 的写法：制品入口保持单一路径，
    测试侧的包身份由 `tests/conftest.py` 伪造，而不是让生产代码多一条
    宿主永远不会走的分支。
    """

    tree = _parse(PLUGIN)
    module_imports = [
        node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    def _targets(node: ast.ImportFrom, prefix: str) -> bool:
        return node.module is not None and (
            node.module == prefix or node.module.startswith(f"{prefix}.")
        )

    relative = [node for node in module_imports if node.level > 0 and _targets(node, "bangumi")]
    absolute = [node for node in module_imports if node.level == 0 and _targets(node, "bangumi")]
    assert relative, "plugin.py 必须有 `from .bangumi.…` 形式"
    assert absolute == [], f"plugin.py 不得再有绝对自导入: {len(absolute)} 处"

    # 不得用 __package__ 分支或 try/except ImportError 分流。
    if_guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "__package__"
    ]
    assert if_guards == [], "plugin.py 不应包含 __package__ 导入分支"

    try_handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(
            handler.type is not None
            and isinstance(handler.type, ast.Name)
            and handler.type.id == "ImportError"
            for handler in node.handlers
        )
    ]
    assert try_handlers == [], (
        "不得用 try/except ImportError 分流：它会掩盖 bangumi 包内部的真实导入错误"
    )


def test_tests_expose_the_repo_as_a_package() -> None:
    """文档 4.1：测试通过伪造包身份让相对导入生效，而不是 import 顶层 plugin。"""

    conftest = (REPO / "tests/conftest.py").read_text(encoding="utf-8")

    assert "bangumi_test_plugin" in conftest, (
        "tests/conftest.py 必须注册伪包，测试才能用相对导入的 plugin.py"
    )
    assert "__path__" in conftest


# ---------------------------------------------------------------------------
# 4.2 ServiceKey 名字必须与宿主提供方逐字一致
# ---------------------------------------------------------------------------

EXPECTED_SERVICE_NAMES: Mapping[str, str] = {
    "TOOLS": "tools.v1",
    "INSTALLED_ASSETS": "assets.installed.v1",
    "CREDENTIALS": "core.credentials",
    "TIMERS": "timers.v1",
    "ALERT_SOURCE": "eventmail.alert_source.v2",
    "CONTEXT_SOURCE": "eventmail.context_source.v2",
}


def test_host_contract_declares_the_current_service_names() -> None:
    """文档 4.2 + 3.2：ServiceKey 的身份就是 name 字符串，名字不一致 = 依赖永远 PENDING。"""

    from bangumi import _host_contract as contract

    actual = {
        symbol: getattr(contract, symbol).name
        for symbol in EXPECTED_SERVICE_NAMES
    }
    assert actual == dict(EXPECTED_SERVICE_NAMES)


def test_host_contract_does_not_keep_retired_core_keys() -> None:
    """文档 3.2：`core.installed_assets.v1` / `core.timers` 已不在宿主的 core 表里。"""

    from bangumi import _host_contract as contract

    retired = {
        "core.installed_assets.v1",
        "core.timers",
        "eventmail.alert_source.v1",
        "eventmail.context_source.v1",
    }
    declared = {
        value
        for name, value in vars(contract).items()
        if name.endswith("_SERVICE_NAME") or name.isupper()
    }
    assert declared.isdisjoint(retired), (
        f"仍残留已退役的 key: {sorted(declared & retired)}"
    )


def test_service_name_constants_match_the_keys_built_from_them() -> None:
    """名字常量与由它构造的 key 必须一致，避免两处分别维护漂移。"""

    from bangumi import _host_contract as contract

    for symbol, expected in EXPECTED_SERVICE_NAMES.items():
        constant = f"{symbol}_SERVICE_NAME"
        if hasattr(contract, constant):
            assert getattr(contract, constant) == expected, constant


# ---------------------------------------------------------------------------
# 4.3 / 4.4 工具注册面与结果值类型
# ---------------------------------------------------------------------------


def test_tool_catalog_service_protocol_drops_retired_parameters() -> None:
    """文档 4.4 + 3.3：宿主 register 已删 `risk` / `search_hint`，窄 Protocol 必须同步。"""

    from bangumi_test_plugin.bangumi._host_contract import ToolCatalogService

    parameters = set(inspect.signature(ToolCatalogService.register).parameters)

    assert "risk" not in parameters, "risk 已从宿主 register 删除"
    assert "search_hint" not in parameters, "search_hint 已从宿主 register 删除"
    for required in ("name", "description", "parameters", "open", "idempotent"):
        assert required in parameters, f"register 缺少宿主仍接受的参数: {required}"


def test_tool_catalog_service_protocol_exposes_parallel_flag() -> None:
    """文档 4.4：只读工具应能声明 parallel，宿主按它做重叠调度。"""

    from bangumi_test_plugin.bangumi._host_contract import ToolCatalogService

    assert "parallel" in inspect.signature(ToolCatalogService.register).parameters


def test_tool_results_expose_outcome_and_parts() -> None:
    """文档 4.3：`ResultLike` 的最小面是 outcome + parts。

    真实类型校验需要宿主源码，见 `tests/test_upstream_contract.py`；
    这里只确认本地结果对象已具备宿主会读取的两个字段。
    """

    from bangumi_test_plugin.bangumi.tools import tool_error, tool_success

    success = tool_success({"ok": True})
    failure = tool_error("boom")

    assert success.outcome == "success"
    assert failure.outcome == "error"
    for result in (success, failure):
        assert isinstance(result.parts, tuple), "parts 必须是 tuple，宿主会直接 tuple() 它"
        assert result.parts, "结果必须至少有一个内容块"


def test_tool_result_text_parts_carry_the_payload_as_text() -> None:
    """文档 4.3：ContentPart(kind, value)，kind 为 "text"，value 是字符串。"""

    from bangumi_test_plugin.bangumi.tools import tool_success

    result = tool_success({"total": 1})
    part = result.parts[0]

    assert part.kind == "text"
    assert isinstance(part.value, str)
    assert '"total":1' in part.value


def test_no_stub_result_classes_remain_in_tools_module() -> None:
    """文档 4.3：`_TextPart` / `_ToolResult` 应删除，不留第二套结果类型。"""

    from bangumi import tools

    assert not hasattr(tools, "_TextPart")
    assert not hasattr(tools, "_ToolResult")


# ---------------------------------------------------------------------------
# 4.5 eventmail v2：异步报告与显式释放
# ---------------------------------------------------------------------------


def test_alert_source_protocol_reports_asynchronously() -> None:
    """文档 4.5 + 3.5：v2 的 `report` / `status` 是 async；同步调用只会拿到未 await 的协程。"""

    from bangumi_test_plugin.bangumi._host_contract import (
        BoundAlertSource,
        BoundContextSource,
    )

    for protocol in (BoundAlertSource, BoundContextSource):
        assert inspect.iscoroutinefunction(protocol.report), (
            f"{protocol.__name__}.report 必须是 async def"
        )
    assert inspect.iscoroutinefunction(BoundAlertSource.status)


def test_alert_bridge_tick_is_asynchronous() -> None:
    """文档 4.5：tick 必须是 async，不能再被放进 `asyncio.to_thread`。"""

    from bangumi_test_plugin.bangumi.alert_source import (
        AlertBinding,
        BangumiAlertBridge,
    )

    assert inspect.iscoroutinefunction(BangumiAlertBridge.tick)
    assert inspect.iscoroutinefunction(AlertBinding.report)


def test_anime_push_loop_awaits_alert_sync_instead_of_offloading() -> None:
    """文档 4.5：循环必须 await alert_sync，不能在 to_thread 里跑协程。"""

    source = (REPO / "bangumi/anime_push_loop.py").read_text(encoding="utf-8")

    assert "to_thread(self.alert_sync)" not in source, (
        "alert_sync 已改为协程，放进线程池不会执行"
    )
    assert "await self.alert_sync()" in source


def test_alert_sync_type_is_awaitable() -> None:
    """文档 4.5：alert_sync 的类型应是返回 Awaitable 的可调用对象。"""

    from bangumi_test_plugin.bangumi.anime_push_loop import AnimeUpdateLoop

    hints = get_type_hints(AnimeUpdateLoop)
    assert "alert_sync" in hints
    text = str(hints["alert_sync"])
    assert "Awaitable" in text or "Coroutine" in text, text


# ---------------------------------------------------------------------------
# 4.6 配置入口：插件自有离线命令
# ---------------------------------------------------------------------------


def test_plugin_declares_configure_entrypoint() -> None:
    """文档 4.6：configure.py 通过 entrypoints 字面量声明，宿主按 AST 读取。"""

    tree = _parse(PLUGIN)
    declarations = _top_level_assignments(tree, "entrypoints")

    assert len(declarations) == 1, "entrypoints 必须是单次顶层声明"
    value = declarations[0]
    assert isinstance(value, ast.Dict), "entrypoints 必须是字面 dict"
    entries = {
        key.value: target.value
        for key, target in zip(value.keys, value.values)
        if isinstance(key, ast.Constant) and isinstance(target, ast.Constant)
    }
    assert entries.get("bangumi-configure") == "configure.main"


def test_configure_entrypoint_name_is_accepted_by_host_rules() -> None:
    """文档 4.6：命令名必须匹配宿主 `[a-z][a-z0-9-]*`，目标匹配 `module.function`。"""

    import re

    tree = _parse(PLUGIN)
    value = _top_level_assignments(tree, "entrypoints")[0]
    assert isinstance(value, ast.Dict)
    for key, target in zip(value.keys, value.values):
        assert isinstance(key, ast.Constant) and isinstance(target, ast.Constant)
        assert re.fullmatch(r"[a-z][a-z0-9-]*", key.value), key.value
        assert re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", target.value), target.value


def test_configure_main_matches_the_host_command_signature() -> None:
    """文档 4.6：宿主以 `function(arguments, workspace=…, config_path=…)` 调用并校验返回 int。"""

    import importlib

    configure = importlib.import_module("bangumi_test_plugin.configure")

    signature = inspect.signature(configure.main)
    parameters = signature.parameters

    positional = [
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert positional, "main 必须接收 arguments（可带默认值）"
    assert "workspace" in parameters, "必须有 workspace 关键字参数"
    assert "config_path" in parameters, "必须有 config_path 关键字参数"
    for name in ("workspace", "config_path"):
        assert (
            parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            or parameters[name].default is not inspect.Parameter.empty
        ), f"{name} 不能被当作必需位置参数"


def test_configure_reads_the_workspace_instead_of_the_plugin_env() -> None:
    """文档 4.6：`main.py setup` 不再注入 AKASHIC_PLUGIN_DATA_DIR，
    数据目录必须由 workspace 推导（plugin-data/<name>-<marketplace>）。
    """

    source = (REPO / "configure.py").read_text(encoding="utf-8")

    assert "AKASHIC_PLUGIN_DATA_DIR" not in source, (
        "该环境变量只在插件子进程里注入，命令不再经过它"
    )
    assert "plugin-data" in source


def test_configure_verifies_the_written_input(tmp_path: Path) -> None:
    """文档风险 5：写完必须回读自校验，不能只看 save 是否抛出异常。"""

    source = (REPO / "configure.py").read_text(encoding="utf-8")
    assert "load_config" in source, "写完应回读 load_config 做格式自校验"


# ---------------------------------------------------------------------------
# 版本与文档一致性
# ---------------------------------------------------------------------------


def test_version_is_bumped_for_the_contract_change() -> None:
    """文档 5：契约语义变了，0.6.x 的语义无法在新宿主上成立。"""

    from bangumi_test_plugin import plugin
    from bangumi_test_plugin.bangumi.settings import DEFAULT_USER_AGENT

    assert plugin.version == "0.7.0"
    assert "0.7.0" in DEFAULT_USER_AGENT, "User-Agent 里也要同步版本"
