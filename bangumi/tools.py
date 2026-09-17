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

from ._host_contract import ContentPart, ToolResult, tool_name_error

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
    """工具无法完成本次调用；错误正文会交给调用方。"""


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
    """把一次工具调用映射为宿主工具结果。"""

    def __call__(self, arguments: Mapping[str, object]) -> ToolResult: ...


class Invoker(Protocol):
    """运行时入口：按方法名执行领域调用并返回工具结果。"""

    async def __call__(
        self,
        method: str,
        *args: object,
        **kwargs: object,
    ) -> ToolResult: ...


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


@dataclass(frozen=True)
class _TextPart:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class _ToolResult:
    outcome: Literal["success", "denied", "error", "interrupted"]
    parts: tuple[ContentPart, ...]


def tool_error(message: str) -> ToolResult:
    """构造一个 error 结果；正文是给模型看的中文说明。"""

    return _ToolResult("error", (_TextPart(message),))


def tool_success(payload: object) -> ToolResult:
    """把领域结果包装成 success 结果正文。"""

    return _ToolResult("success", (_TextPart(_json_text(payload)),))


def _require_text(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"{key} 必须是非空字符串")
    return value.strip()


def _require_int(
    arguments: Mapping[str, object],
    key: str,
    *,
    minimum: int = 1,
) -> int:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ToolExecutionError(f"{key} 必须是大于等于 {minimum} 的整数")
    return value


def _optional_int(
    arguments: Mapping[str, object],
    key: str,
    *,
    default: int,
    minimum: int = 0,
) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ToolExecutionError(f"{key} 必须是大于等于 {minimum} 的整数")
    return value


def _enum_text(
    arguments: Mapping[str, object],
    key: str,
    allowed: Sequence[str],
    *,
    default: str,
) -> str:
    value = arguments.get(key)
    if value is None:
        value = default
    if not isinstance(value, str) or value not in allowed:
        raise ToolExecutionError(f"{key} 必须是 {list(allowed)} 之一")
    return value


def _optional_rating(
    arguments: Mapping[str, object],
    key: str,
) -> int | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolExecutionError(f"{key} 必须是整数或 null")
    return value


def _string_list(arguments: Mapping[str, object], key: str) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, list) or not value:
        raise ToolExecutionError(f"{key} 必须是非空字符串数组")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolExecutionError(f"{key} 只能包含非空字符串")
        result.append(item.strip())
    return result


def _handler(name: str, invoke: Invoker) -> ToolExecutor:
    """把工具名绑定到运行时方法；参数在这里显式校验。

    校验失败、凭据不可用或领域错误都返回 error 结果，交给模型说明原因。
    """

    async def execute(arguments: Mapping[str, object]) -> ToolResult:
        try:
            if name == "list_collections":
                return await invoke(
                    "list_collections",
                    subject_type=_subject_type(arguments),
                    status=_collection_status(arguments),
                )
            if name == "count_collections":
                return await invoke(
                    "count_collections",
                    subject_type=_subject_type(arguments),
                    status=_collection_status(arguments),
                )
            if name == "continue_collection_query":
                return await invoke(
                    "continue_collection_query",
                    query_id=_require_text(arguments, "query_id"),
                )
            if name == "get_collection_status":
                return await invoke(
                    "get_collection_status",
                    subject_id=_require_int(arguments, "subject_id"),
                )
            if name == "prepare_collection_query":
                return await invoke(
                    "prepare_collection_query",
                    subject_type=_subject_type(arguments),
                    status=_collection_status(arguments),
                    operation=_enum_text(
                        arguments,
                        "operation",
                        QUERY_OPERATION_ENUM,
                        default="list_all",
                    ),
                    min_rating=_optional_rating(arguments, "min_rating"),
                    max_rating=_optional_rating(arguments, "max_rating"),
                    requested_count=_optional_int(
                        arguments,
                        "requested_count",
                        default=10,
                        minimum=1,
                    ),
                    return_all_matches=_flag(arguments, "return_all_matches"),
                )
            if name == "execute_prepared_collection_query":
                return await invoke(
                    "execute_prepared_collection_query",
                    confirmation_id=_require_text(arguments, "confirmation_id"),
                    confirmation_text=_require_text(
                        arguments,
                        "confirmation_text",
                    ),
                )
            if name == "prepare_collection_status_update":
                return await invoke(
                    "prepare_collection_status_update",
                    subject_id=_require_int(arguments, "subject_id"),
                    status=_enum_text(
                        arguments,
                        "status",
                        ("watching", "completed"),
                        default="",
                    ),
                )
            if name == "prepare_anime_progress_update":
                return await invoke(
                    "prepare_anime_progress_update",
                    subject_id=_require_int(arguments, "subject_id"),
                    episode_number=_require_int(arguments, "episode_number"),
                )
            if name == "commit_prepared_update":
                return await invoke(
                    "commit_prepared_update",
                    confirmation_id=_require_text(arguments, "confirmation_id"),
                    confirmation_text=_require_text(
                        arguments,
                        "confirmation_text",
                    ),
                )
            if name == "get_anime_update_alerts":
                return await invoke(
                    "fetch_pending_alerts",
                    offset=_optional_int(arguments, "offset", default=0),
                    limit=_optional_int(
                        arguments,
                        "limit",
                        default=50,
                        minimum=1,
                    ),
                )
            if name == "acknowledge_anime_update_alerts":
                return await invoke(
                    "acknowledge_anime_update_alerts",
                    event_ids=_string_list(arguments, "event_ids"),
                )
        except ToolExecutionError as error:
            return tool_error(str(error))
        raise ToolExecutionError(f"未知工具: {name}")

    return execute


def _subject_type(arguments: Mapping[str, object]) -> str:
    return _enum_text(
        arguments,
        "subject_type",
        SUBJECT_TYPE_ENUM,
        default="all",
    )


def _collection_status(arguments: Mapping[str, object]) -> str:
    return _enum_text(
        arguments,
        "status",
        COLLECTION_STATUS_ENUM,
        default="all",
    )


def _flag(arguments: Mapping[str, object], key: str) -> bool:
    value = arguments.get(key, False)
    if not isinstance(value, bool):
        raise ToolExecutionError(f"{key} 必须是布尔值")
    return value


def build_tool_catalog(
    *,
    invoke: Invoker,
) -> ToolCatalog:
    """构造绑定到运行时调用的工具目录。"""

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
                execute=_handler("list_collections", invoke),
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
                execute=_handler("continue_collection_query", invoke),
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
                execute=_handler("count_collections", invoke),
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
                execute=_handler("get_collection_status", invoke),
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
                execute=_handler("prepare_collection_query", invoke),
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
                execute=_handler("execute_prepared_collection_query", invoke),
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
                execute=_handler("prepare_collection_status_update", invoke),
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
                execute=_handler("prepare_anime_progress_update", invoke),
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
                execute=_handler("commit_prepared_update", invoke),
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
                execute=_handler("get_anime_update_alerts", invoke),
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
                execute=_handler("acknowledge_anime_update_alerts", invoke),
            ),
        )
    )


__all__ = [
    "COLLECTION_STATUS_ENUM",
    "QUERY_OPERATION_ENUM",
    "SUBJECT_TYPE_ENUM",
    "Invoker",
    "ToolCatalog",
    "ToolEntry",
    "ToolExecutionError",
    "ToolExecutor",
    "build_tool_catalog",
    "tool_error",
    "tool_success",
]
