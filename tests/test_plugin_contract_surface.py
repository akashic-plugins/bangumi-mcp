from __future__ import annotations

import ast
from pathlib import Path

from tests._catalog import discovery_catalog

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin.py"
EXPECTED_TOOLS = {
    "acknowledge_anime_update_alerts",
    "commit_prepared_update",
    "continue_collection_query",
    "count_collections",
    "execute_prepared_collection_query",
    "get_anime_update_alerts",
    "get_collection_status",
    "list_collections",
    "prepare_anime_progress_update",
    "prepare_collection_query",
    "prepare_collection_status_update",
}


CATALOG = discovery_catalog()


def _plugin_tree() -> ast.Module:
    return ast.parse(PLUGIN.read_text(encoding="utf-8"), filename=str(PLUGIN))


def _literal_assignments(tree: ast.Module) -> dict[str, object]:
    values: dict[str, object] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        else:
            continue
        names = [
            target.id
            for target in targets
            if isinstance(target, ast.Name)
            and target.id in {"name", "version", "api_version"}
        ]
        if not names:
            continue
        assert len(targets) == 1 and len(names) == 1, "身份必须是单次直接赋值"
        assert isinstance(value, ast.Constant), "身份必须是字面量"
        values[names[0]] = value.value
    return values


def _apply_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "apply"
    ]
    assert len(functions) == 1, "plugin.py 必须只导出一个 apply"
    function = functions[0]
    assert isinstance(function, ast.AsyncFunctionDef), "apply 必须是 async"
    return function


def test_plugin_declares_v3_identity_literals() -> None:
    values = _literal_assignments(_plugin_tree())

    assert values["api_version"] == 3
    assert values["name"] == "bangumi"
    assert values["version"] == "0.6.0"


def test_plugin_identity_matches_host_ast_loader_rules() -> None:
    """复刻宿主 loader 的身份读取规则：三个顶层单次字面量，且 API 必须为 3。"""

    fields = {"name", "version", "api_version"}
    values: dict[str, object] = {}
    for statement in _plugin_tree().body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        else:
            continue
        names = [
            target.id
            for target in targets
            if isinstance(target, ast.Name) and target.id in fields
        ]
        if not names:
            continue
        assert len(targets) == 1 and len(names) == 1, "身份必须是单次直接赋值"
        assert isinstance(value, ast.Constant), "身份必须是字面量"
        assert names[0] not in values, f"身份重复赋值: {names[0]}"
        values[names[0]] = value.value

    assert sorted(values) == sorted(fields), "身份字段必须齐全"
    assert type(values["api_version"]) is int
    assert values["api_version"] == 3


def test_plugin_apply_accepts_exactly_one_positional_argument() -> None:
    function = _apply_function(_plugin_tree())

    positional = function.args.posonlyargs + function.args.args
    assert len(positional) == 1
    assert positional[0].arg == "ctx"
    assert function.args.vararg is None
    assert function.args.kwarg is None


def test_plugin_has_no_v2_protocol_surface() -> None:
    tree = _plugin_tree()
    source = PLUGIN.read_text(encoding="utf-8")

    assert not any(
        isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "Plugin"
            for base in node.bases
        )
        for node in tree.body
    ), "V3 插件不导出 Plugin 子类"
    for forbidden in (
        "class BangumiPlugin",
        "skill_roots",
        "drift_skill_roots",
        "mcp_servers",
        "proactive_sources",
        "config.local.toml",
    ):
        assert forbidden not in source


def test_plugin_declares_skills_and_tools() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert '"skills", "skills"' in source
    assert "runtime.catalog()" in source
    assert "parse_settings" in source
    assert (REPO / "skills/bangumi/SKILL.md").is_file()
    assert (REPO / "requirements.txt").is_file()


def test_plugin_injects_required_service_keys() -> None:
    tree = _plugin_tree()
    inject = next(
        (
            statement
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "inject"
        ),
        None,
    )

    assert inject is not None, "plugin.py 必须声明 inject"
    assert isinstance(inject.value, ast.Tuple)
    names = [
        element.id
        for element in inject.value.elts
        if isinstance(element, ast.Name)
    ]
    assert names == [
        "TOOLS",
        "INSTALLED_ASSETS",
        "CREDENTIALS",
        "TIMERS",
    ]


def test_tool_catalog_lists_exactly_the_public_tools() -> None:
    assert set(CATALOG.names) == EXPECTED_TOOLS
    assert len(CATALOG.names) == len(set(CATALOG.names))


def test_tool_catalog_schemas_are_object_shaped() -> None:
    for entry in CATALOG.entries:
        assert entry.parameters["type"] == "object", entry.name
        assert entry.description, entry.name
        assert entry.risk in {
            "read-only",
            "read-write",
            "external-side-effect",
        }, entry.name


def test_only_commit_tool_has_external_side_effect() -> None:
    risk_by_name = {entry.name: entry.risk for entry in CATALOG.entries}

    assert risk_by_name["commit_prepared_update"] == "external-side-effect"
    assert risk_by_name["acknowledge_anime_update_alerts"] == "read-write"
    assert all(
        risk == "read-only"
        for name, risk in risk_by_name.items()
        if name
        not in {"commit_prepared_update", "acknowledge_anime_update_alerts"}
    )


def test_list_collections_schema_preserves_display_boundaries() -> None:
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


def test_prepare_query_schema_matches_confirmation_contract() -> None:
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
    assert set(schema.get("required", ())) == set()

    continue_schema = CATALOG["continue_collection_query"].parameters
    assert set(continue_schema["properties"]) == {"query_id"}
    assert continue_schema["required"] == ["query_id"]

    count_schema = CATALOG["count_collections"].parameters
    assert set(count_schema["properties"]) == {"subject_type", "status"}


def test_confirmation_tool_schemas_require_literal_text() -> None:
    for name in (
        "execute_prepared_collection_query",
        "commit_prepared_update",
    ):
        schema = CATALOG[name].parameters

        assert set(schema["properties"]) == {
            "confirmation_id",
            "confirmation_text",
        }
        assert set(schema["required"]) == {
            "confirmation_id",
            "confirmation_text",
        }


def test_alert_tool_schemas_match_store_limits() -> None:
    fetch_schema = CATALOG["get_anime_update_alerts"].parameters
    assert set(fetch_schema["properties"]) == {"offset", "limit"}
    assert fetch_schema["properties"]["limit"]["maximum"] == 50

    ack_schema = CATALOG["acknowledge_anime_update_alerts"].parameters
    assert set(ack_schema["properties"]) == {"event_ids", "feedback"}
    assert ack_schema["required"] == ["event_ids"]


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
    assert "模型不要声称已经主动通知过用户" in skill
