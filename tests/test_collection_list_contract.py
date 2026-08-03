from __future__ import annotations

from pathlib import Path

from src.server import create_mcp_server


REPO = Path(__file__).resolve().parents[1]


def test_mcp_registers_collection_list_tool(tmp_path: Path) -> None:
    server = create_mcp_server(tmp_path)

    tools = server._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert names == {
        "commit_prepared_update",
        "get_collection_status",
        "list_collections",
        "prepare_anime_progress_update",
        "prepare_collection_status_update",
    }
    schema = next(tool.parameters for tool in tools if tool.name == "list_collections")
    properties = schema["properties"]
    assert properties["limit"]["default"] == 10
    assert properties["offset"]["default"] == 0
    assert properties["subject_type"]["enum"] == [
        "all",
        "book",
        "anime",
        "music",
        "game",
        "real",
    ]
    assert properties["status"]["enum"] == [
        "all",
        "wish",
        "completed",
        "watching",
        "on_hold",
        "dropped",
    ]


def test_skill_preserves_collection_list_boundaries() -> None:
    skill = (REPO / "skills/bangumi/SKILL.md").read_text(encoding="utf-8")

    assert "list_collections" in skill
    assert "默认 `limit=10`" in skill
    assert "`next_offset`" in skill
    assert "reported_episode_progress" in skill
    assert "总计不超过 200 条" in skill
