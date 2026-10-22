"""宿主（akashic-agent）可用性探测。

本仓库是**插件**仓库，不含宿主源码（`agent/`、`plugins/` 都在 akashic-agent 里）。
有些断言只能对着宿主真实类型跑（例如「工具结果必须是宿主的 `ContentPart`」），
另一些只需检查本地声明形状（例如 ServiceKey 的 name 字符串）。

这里提供统一的探测与跳过标记，避免每个测试模块各写一套路径猜测：

- 优先用环境变量 `AKASHIC_AGENT_ROOT`（CI 里 checkout 宿主后设置）；
- 否则尝试常见的同级目录 `../akashic-agent`；
- 都没有时，`requires_host` 的测试 `skip`，而不是失败。
"""

from __future__ import annotations

import importlib
import os
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]

_CANDIDATE_ROOTS: tuple[Path, ...] = (
    Path(os.environ.get("AKASHIC_AGENT_ROOT", "")).expanduser()
    if os.environ.get("AKASHIC_AGENT_ROOT", "").strip()
    else None,  # type: ignore[arg-type]
    REPO.parent / "akashic-agent",
)


def agent_root() -> Path | None:
    """返回可用的宿主仓库根目录；没有则 None。"""

    for candidate in _CANDIDATE_ROOTS:
        if candidate is None:
            continue
        root = Path(candidate)
        if (root / "agent" / "plugin_composition" / "__init__.py").is_file() and (
            root / "plugins" / "ledger" / "contract.py"
        ).is_file():
            return root.resolve()
    return None


@lru_cache(maxsize=1)
def _host_import_works() -> bool:
    """宿主源码能真正 import 吗（含它的运行时依赖）。

    只有源码、缺依赖（例如 pythonjsonlogger）时不代表能跑：这种情况下
    与其让每个用例报 ModuleNotFoundError，不如整体跳过，并提示换解释器。
    """

    root = agent_root()
    if root is None:
        return False
    text = str(root)
    if text not in sys.path:
        sys.path.append(text)
    try:
        importlib.import_module("agent.plugin_composition")
        importlib.import_module("plugins.tools.contract")
        importlib.import_module("plugins.ledger.contract")
    except Exception:  # noqa: BLE001 - 任何导入失败都表示宿主不可用
        return False
    return True


def host_skip_reason() -> str | None:
    """None 表示可用；否则是跳过原因。"""

    if agent_root() is None:
        return "未找到宿主源码；设置 AKASHIC_AGENT_ROOT 指向 akashic-agent"
    if not _host_import_works():
        return (
            "宿主源码存在但无法在其依赖下导入；"
            "请用宿主的 venv 运行本文件（例如 akashic-agent/.venv/bin/python -m pytest）"
        )
    return None


def import_host(module: str) -> ModuleType:
    """在宿主源码可用时导入宿主模块；不可用直接抛 ImportError（由标记负责跳过）。

    宿主路径**追加到 sys.path 末尾**，不能插到最前：宿主仓库也有一个 `tests/` 目录，
    插到前面会让 `import tests._host` 解析到宿主的 tests 包，导致本仓库的
    tests 辅助模块全部不可见。
    """

    root = agent_root()
    if root is None:
        raise ImportError("宿主源码不可用；请设置 AKASHIC_AGENT_ROOT")
    text = str(root)
    if text not in sys.path:
        sys.path.append(text)
    _ensure_local_tests_first()
    return importlib.import_module(module)


def _ensure_local_tests_first() -> None:
    """保证 `tests` 这个 namespace 包优先解析到本仓库的 tests 目录。"""

    local = str(REPO)
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)


def host_available() -> bool:
    return agent_root() is not None


# 供 pytest 标记使用：没有可用的宿主时跳过，而不是把插件仓库的 CI 判红。
requires_host = __import__("pytest").mark.skipif(
    host_skip_reason() is not None,
    reason=host_skip_reason() or "",
)
