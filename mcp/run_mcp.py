#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def _plugin_data_dir() -> Path:
    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise RuntimeError("bangumi MCP 缺少 AKA_PLUGIN_DATA_DIR")
    data_dir = Path(raw).expanduser().resolve()
    if not data_dir.is_dir():
        raise RuntimeError(f"bangumi 插件数据目录不存在: {data_dir}")
    return data_dir


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    from src.server import create_mcp_server

    create_mcp_server(_plugin_data_dir()).run(transport="stdio")


if __name__ == "__main__":
    main()
