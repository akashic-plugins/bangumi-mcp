from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_plugin_package() -> None:
    """把仓库根注册成伪包，让 ``plugin.py`` 的相对导入在测试里成立。

    对齐 ``akashic-plugins/fitbit-mcp``：制品入口只写 ``from .bangumi.…``，
    测试侧给它一个包身份，而不是让生产代码多一条宿主永远不会走的分支。
    注册后测试写 ``from bangumi_test_plugin import plugin``。
    """

    if "bangumi_test_plugin" in sys.modules:
        return
    package = types.ModuleType("bangumi_test_plugin")
    package.__path__ = [str(REPO_ROOT)]  # type: ignore[attr-defined]
    package.__package__ = "bangumi_test_plugin"
    sys.modules["bangumi_test_plugin"] = package


def _agent_root() -> Path | None:
    """宿主源码根：显式 `AKASHIC_AGENT_ROOT` 优先，其次同级 checkout。"""

    candidates: list[Path] = []
    configured = os.environ.get("AKASHIC_AGENT_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(REPO_ROOT.parent / "akashic-agent")
    for candidate in candidates:
        if (candidate / "plugins" / "ledger" / "contract.py").is_file():
            return candidate.resolve()
    return None


def _install_host_stub() -> None:
    """离线测试的最小宿主 SDK。

    真实运行时始终使用 Akashic 自己的 ``agent.plugin_composition``；这里只在
    本地没有宿主代码时提供同名 key 容器与身份类，让 plugin.py 可以被导入。

    有真实宿主时优先用它：桩只实现本插件用到的几个符号，拿它跑
    ``test_upstream_contract.py`` 会得到假阴性。宿主路径**追加**到 sys.path 末尾，
    并且不改动 `tests` 的解析顺序——宿主仓库也有 `tests/` 目录，前置会遮蔽它。
    """

    if "agent.plugin_composition" in sys.modules:
        return

    root = _agent_root()
    if root is not None:
        text = str(root)
        if text not in sys.path:
            sys.path.append(text)
        try:
            import agent.plugin_composition
            import plugins.ledger.contract
        except ModuleNotFoundError:
            pass
        else:
            return

    try:
        import agent.plugin_composition
        import plugins.ledger.contract  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        return

    _T = TypeVar("_T")
    _P = TypeVar("_P")
    _R = TypeVar("_R")

    class ServiceKey(Generic[_T]):
        def __init__(self, name: str) -> None:
            self.name = name

        def __repr__(self) -> str:
            return f"ServiceKey({self.name!r})"

        def __eq__(self, other: object) -> bool:
            return isinstance(other, ServiceKey) and other.name == self.name

        def __hash__(self) -> int:
            return hash(self.name)

    class SerialEventKey(Generic[_P, _R]):
        def __init__(self, name: str) -> None:
            self.name = name

        def __repr__(self) -> str:
            return f"SerialEventKey({self.name!r})"

    @dataclass(frozen=True)
    class CredentialRef:
        path: tuple[str, ...]

    composition = types.ModuleType("agent.plugin_composition")
    composition.ServiceKey = ServiceKey  # type: ignore[attr-defined]
    composition.SerialEventKey = SerialEventKey  # type: ignore[attr-defined]
    composition.CredentialRef = CredentialRef  # type: ignore[attr-defined]
    composition.Effect = object  # type: ignore[attr-defined]
    composition.Context = object  # type: ignore[attr-defined]
    composition.RUNTIME_STARTED = SerialEventKey("runtime.started")  # type: ignore[attr-defined]
    composition.RUNTIME_STOPPING = SerialEventKey("runtime.stopping")  # type: ignore[attr-defined]

    agent = types.ModuleType("agent")
    agent.plugin_composition = composition  # type: ignore[attr-defined]
    sys.modules.setdefault("agent", agent)
    sys.modules["agent.plugin_composition"] = composition


_install_host_stub()


_install_plugin_package()
