from __future__ import annotations

import ast
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1] / "plugin.py"


def test_plugin_declares_v2_mcp_and_skill_roots() -> None:
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    plugin_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BangumiPlugin"
    )
    assignments = {
        target.id: ast.literal_eval(item.value)
        for item in plugin_class.body
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance((target := item.targets[0]), ast.Name)
        and isinstance(item.value, ast.Constant)
    }

    assert assignments["api_version"] == 2
    assert assignments["name"] == "bangumi"
    assert assignments["version"] == "0.2.1"
    assert all(
        not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        or item.name != "initialize"
        for item in plugin_class.body
    )
    source = PLUGIN.read_text(encoding="utf-8")
    assert 'return ("skills",)' in source
    assert 'command=("python", "mcp/run_mcp.py")' in source
