from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_host_stub() -> None:
    """离线测试的最小宿主 SDK。

    真实运行时始终使用 Akashic 自己的 ``agent.plugin_composition``；这里只在
    本地没有宿主代码时提供同名 key 容器与身份类，让 plugin.py 可以被导入。
    """

    if "agent.plugin_composition" in sys.modules:
        return
    try:
        import agent.plugin_composition
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
