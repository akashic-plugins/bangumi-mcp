"""Bangumi 工具目录：名字、描述、JSON Schema 与执行入口。

V3 没有 MCP 注册面，工具直接在 ``apply(ctx)`` 里注册进宿主的 ``tools.v1``。
本模块是工具发现合同的单一真源：``plugin.py`` 按 ``TOOL_CATALOG`` 注册，
测试直接校验这份目录，不依赖宿主运行时。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ._host_contract import tool_name_error

Risk = Literal["read-only", "read-write", "external-side-effect"]

SUBJECT_TYPE_ENUM = ["all", "book", "anime", "music", "game", "real"]
COLLECTION_STATUS_ENUM = [
    "all",
    "wish",
    "completed",
    "watching",
    "on_hold",
    "dropped",
]
QUERY_OPERATION_ENUM = ["list_all", "filter", "analyze"]


class ToolExecutionError(RuntimeError):
    """工具还没有接到领域实现，或领域实现拒绝本次调用。"""


@dataclass(frozen=True)
class ToolEntry:
    """一个工具的静态发现合同与执行入口。"""

    name: str
    description: str
    parameters: Mapping[str, object]
    risk: Risk
    idempotent: bool
    execute: ToolExecutor


class ToolExecutor(Protocol):
    """把一次工具调用映射为 JSON 结果正文。"""

    def __call__(self, arguments: Mapping[str, object]) -> object: ...


@dataclass(frozen=True)
class ToolCatalog:
    """插件拥有的工具目录；构造时自检，避免非法名字进入宿主注册。"""

    entries: tuple[ToolEntry, ...]
    _by_name: dict[str, ToolEntry] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        names: dict[str, ToolEntry] = {}
        for entry in self.entries:
            error = tool_name_error(entry.name)
            if error is not None:
                raise ValueError(f"{error}: {entry.name!r}")
            if entry.name in names:
                raise ValueError(f"工具名重复: {entry.name}")
            if entry.parameters.get("type") != "object":
                raise ValueError(f"工具 parameters 必须是 object schema: {entry.name}")
            if entry.risk not in {"read-only", "read-write", "external-side-effect"}:
                raise ValueError(f"工具 risk 无效: {entry.name}: {entry.risk}")
            names[entry.name] = entry
        object.__setattr__(self, "_by_name", names)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def __getitem__(self, name: str) -> ToolEntry:
        return self._by_name[name]

    def get(self, name: str) -> ToolEntry | None:
        return self._by_name.get(name)


def _schema(
    properties: Mapping[str, object],
    required: Sequence[str] = (),
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _enum(values: Sequence[str], *, default: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "string", "enum": list(values)}
    if default is not None:
        result["default"] = default
    return result


def _json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _unwired(name: str) -> ToolExecutor:
    """Phase 0 占位：工具已可发现，但领域实现要到 Phase 2 才接入。"""

    def execute(arguments: Mapping[str, object]) -> object:
        del arguments
        raise ToolExecutionError(
            f"工具 {name} 的领域实现尚未接入（Phase 2）"
        )

    return execute


def build_tool_catalog(
    *,
    execute: ToolExecutor | None = None,
) -> ToolCatalog:
    """构造工具目录；``execute`` 为空时全部工具使用未接入占位。"""

    def handler(name: str) -> ToolExecutor:
        if execute is None:
            return _unwired(name)
        return execute

    return ToolCatalog(
        entries=(
            ToolEntry(
                name="list_collections",
                description="创建普通收藏查询会话；只读，首页固定 10 条。",
                parameters=_schema(
                    {
                        "subject_type": _enum(SUBJECT_TYPE_ENUM, default="all"),
                        "status": _enum(COLLECTION_STATUS_ENUM, default="all"),
                    }
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("list_collections"),
            ),
            ToolEntry(
                name="continue_collection_query",
                description=(
                    "通过不透明 query_id 继续 10 条展示页；达到 100 条前要求确认。"
                ),
                parameters=_schema(
                    {"query_id": {"type": "string", "minLength": 1}},
                    required=("query_id",),
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("continue_collection_query"),
            ),
            ToolEntry(
                name="count_collections",
                description="只查询当前 Token 用户匹配收藏的总数；只读。",
                parameters=_schema(
                    {
                        "subject_type": _enum(SUBJECT_TYPE_ENUM, default="all"),
                        "status": _enum(COLLECTION_STATUS_ENUM, default="all"),
                    }
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("count_collections"),
            ),
            ToolEntry(
                name="get_collection_status",
                description=(
                    "查询指定 Bangumi 条目的收藏状态及动画逐集观看进度。"
                ),
                parameters=_schema(
                    {"subject_id": {"type": "integer", "minimum": 1}},
                    required=("subject_id",),
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("get_collection_status"),
            ),
            ToolEntry(
                name="prepare_collection_query",
                description="预览完整或大量收藏查询；只计数，绝不执行全量读取。",
                parameters=_schema(
                    {
                        "subject_type": _enum(SUBJECT_TYPE_ENUM, default="all"),
                        "status": _enum(COLLECTION_STATUS_ENUM, default="all"),
                        "operation": _enum(QUERY_OPERATION_ENUM, default="list_all"),
                        "min_rating": {"type": ["integer", "null"]},
                        "max_rating": {"type": ["integer", "null"]},
                        "requested_count": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 10,
                        },
                        "return_all_matches": {"type": "boolean", "default": False},
                    }
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("prepare_collection_query"),
            ),
            ToolEntry(
                name="execute_prepared_collection_query",
                description="执行已确认的固定只读查询计划；禁止与 prepare 同轮调用。",
                parameters=_schema(
                    {
                        "confirmation_id": {"type": "string", "minLength": 1},
                        "confirmation_text": {"type": "string", "minLength": 1},
                    },
                    required=("confirmation_id", "confirmation_text"),
                ),
                risk="read-only",
                idempotent=False,
                execute=handler("execute_prepared_collection_query"),
            ),
            ToolEntry(
                name="prepare_collection_status_update",
                description="预览把条目设为在看或看过；此工具绝不执行 Bangumi 写入。",
                parameters=_schema(
                    {
                        "subject_id": {"type": "integer", "minimum": 1},
                        "status": _enum(["watching", "completed"]),
                    },
                    required=("subject_id", "status"),
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("prepare_collection_status_update"),
            ),
            ToolEntry(
                name="prepare_anime_progress_update",
                description="预览把动画第 1 至 N 集逐集标为看过；此工具绝不写入。",
                parameters=_schema(
                    {
                        "subject_id": {"type": "integer", "minimum": 1},
                        "episode_number": {"type": "integer", "minimum": 1},
                    },
                    required=("subject_id", "episode_number"),
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("prepare_anime_progress_update"),
            ),
            ToolEntry(
                name="commit_prepared_update",
                description=(
                    "执行已预览写入。只能在用户最新消息逐字确认后调用，"
                    "禁止与 prepare 同轮调用。"
                ),
                parameters=_schema(
                    {
                        "confirmation_id": {"type": "string", "minLength": 1},
                        "confirmation_text": {"type": "string", "minLength": 1},
                    },
                    required=("confirmation_id", "confirmation_text"),
                ),
                risk="external-side-effect",
                idempotent=False,
                execute=handler("commit_prepared_update"),
            ),
            ToolEntry(
                name="get_anime_update_alerts",
                description="只读返回本地已到期且尚未 ACK 的放送提醒快照。",
                parameters=_schema(
                    {
                        "offset": {"type": "integer", "minimum": 0, "default": 0},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 50,
                        },
                    }
                ),
                risk="read-only",
                idempotent=True,
                execute=handler("get_anime_update_alerts"),
            ),
            ToolEntry(
                name="acknowledge_anime_update_alerts",
                description="按 event_id 原子确认放送提醒；仅在用户明确要求时调用。",
                parameters=_schema(
                    {
                        "event_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                        },
                        "feedback": {"type": ["string", "null"]},
                    },
                    required=("event_ids",),
                ),
                risk="read-write",
                idempotent=True,
                execute=handler("acknowledge_anime_update_alerts"),
            ),
        )
    )


TOOL_CATALOG = build_tool_catalog()


__all__ = [
    "COLLECTION_STATUS_ENUM",
    "QUERY_OPERATION_ENUM",
    "SUBJECT_TYPE_ENUM",
    "TOOL_CATALOG",
    "ToolCatalog",
    "ToolEntry",
    "ToolExecutionError",
    "ToolExecutor",
    "build_tool_catalog",
]
