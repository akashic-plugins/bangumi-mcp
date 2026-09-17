from __future__ import annotations

from pathlib import Path

from tests._catalog import discovery_catalog

REPO = Path(__file__).resolve().parents[1]
CATALOG = discovery_catalog()


def test_catalog_lists_collection_list_tool_family() -> None:
    names = set(CATALOG.names)

    assert names == {
        "commit_prepared_update",
        "continue_collection_query",
        "count_collections",
        "execute_prepared_collection_query",
        "get_anime_update_alerts",
        "get_collection_status",
        "list_collections",
        "acknowledge_anime_update_alerts",
        "prepare_anime_progress_update",
        "prepare_collection_query",
        "prepare_collection_status_update",
    }


def test_list_collections_parameters_exclude_paging_knobs() -> None:
    schema = CATALOG["list_collections"].parameters
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


def test_continue_collection_query_parameters_are_opaque() -> None:
    schema = CATALOG["continue_collection_query"].parameters

    assert set(schema["properties"]) == {"query_id"}


def test_prepare_collection_query_parameters() -> None:
    schema = CATALOG["prepare_collection_query"].parameters

    assert set(schema["properties"]) == {
        "subject_type",
        "status",
        "operation",
        "min_rating",
        "max_rating",
        "requested_count",
        "return_all_matches",
    }


def test_execute_prepared_collection_query_parameters() -> None:
    schema = CATALOG["execute_prepared_collection_query"].parameters

    assert set(schema["properties"]) == {
        "confirmation_id",
        "confirmation_text",
    }


def test_count_collections_parameters() -> None:
    schema = CATALOG["count_collections"].parameters

    assert set(schema["properties"]) == {"subject_type", "status"}


def test_alert_parameters() -> None:
    fetch_schema = CATALOG["get_anime_update_alerts"].parameters
    assert set(fetch_schema["properties"]) == {"offset", "limit"}

    ack_schema = CATALOG["acknowledge_anime_update_alerts"].parameters
    assert set(ack_schema["properties"]) == {"event_ids", "feedback"}


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
    assert "计划放送" in skill
    assert "不能说某个流媒体" in skill
    assert "插件已记录事件" in skill
