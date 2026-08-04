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
        "continue_collection_query",
        "count_collections",
        "execute_prepared_collection_query",
        "get_collection_status",
        "list_collections",
        "prepare_anime_progress_update",
        "prepare_collection_query",
        "prepare_collection_status_update",
    }
    schema = next(tool.parameters for tool in tools if tool.name == "list_collections")
    properties = schema["properties"]
    assert "limit" not in properties
    assert "offset" not in properties
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
    continue_schema = next(
        tool.parameters for tool in tools if tool.name == "continue_collection_query"
    )
    assert set(continue_schema["properties"]) == {"query_id"}
    prepare_schema = next(
        tool.parameters for tool in tools if tool.name == "prepare_collection_query"
    )
    assert set(prepare_schema["properties"]) == {
        "subject_type",
        "status",
        "operation",
        "min_rating",
        "max_rating",
        "requested_count",
        "return_all_matches",
    }
    execute_schema = next(
        tool.parameters
        for tool in tools
        if tool.name == "execute_prepared_collection_query"
    )
    assert set(execute_schema["properties"]) == {
        "confirmation_id",
        "confirmation_text",
    }
    count_schema = next(
        tool.parameters for tool in tools if tool.name == "count_collections"
    )
    assert set(count_schema["properties"]) == {"subject_type", "status"}


def test_skill_preserves_collection_list_boundaries() -> None:
    skill = (REPO / "skills/bangumi/SKILL.md").read_text(encoding="utf-8")

    assert "list_collections" in skill
    assert "continue_collection_query" in skill
    assert "prepare_collection_query" in skill
    assert "execute_prepared_collection_query" in skill
    assert "count_collections" in skill
    assert "固定返回最多 10 条" in skill
    assert "全部列出" in skill
    assert "每页 50 条" in skill
    assert "`query_id`" in skill
    assert "reported_episode_progress" in skill
    assert "达到或超过 100 条" in skill
    assert "没有 100 或 200 条静默上限" in skill
    assert "prepare 和 execute 禁止在同一轮调用" in skill
