from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from numbers import Real
from typing import Any, Literal

from .client import BangumiApiError, BangumiClient
from .confirmation import ConfirmationStore, PreparedUpdate
from .query import (
    CollectionQueryOperation,
    CollectionQueryRequestOperation,
    CollectionQuerySessionStore,
    ContinueAction,
    PreparedCollectionQuery,
    QueryConfirmationStore,
    QueryStateError,
)


SUBJECT_ANIME = 2
EPISODE_WATCHED = 2
COLLECTION_TYPES = {
    1: "想看",
    2: "看过",
    3: "在看",
    4: "搁置",
    5: "抛弃",
}
SUBJECT_TYPES = {
    1: "书籍",
    2: "动画",
    3: "音乐",
    4: "游戏",
    6: "三次元",
}
STATUS_TARGETS = {
    "watching": (3, "在看"),
    "completed": (2, "看过"),
}
SUBJECT_TYPE_FILTERS = {
    "all": None,
    "book": 1,
    "anime": 2,
    "music": 3,
    "game": 4,
    "real": 6,
}
SUBJECT_TYPE_VALUES = {
    1: "book",
    2: "anime",
    3: "music",
    4: "game",
    6: "real",
}
COLLECTION_STATUS_FILTERS = {
    "all": None,
    "wish": 1,
    "completed": 2,
    "watching": 3,
    "on_hold": 4,
    "dropped": 5,
}
COLLECTION_STATUS_VALUES = {
    1: "wish",
    2: "completed",
    3: "watching",
    4: "on_hold",
    5: "dropped",
}
COLLECTION_DISPLAY_PAGE_SIZE = 10
COLLECTION_BULK_PAGE_SIZE = 50

SubjectTypeFilter = Literal["all", "book", "anime", "music", "game", "real"]
CollectionStatusFilter = Literal[
    "all",
    "wish",
    "completed",
    "watching",
    "on_hold",
    "dropped",
]


class BangumiInputError(ValueError):
    pass


class BangumiService:
    def __init__(
        self,
        client: BangumiClient,
        confirmations: ConfirmationStore,
        query_confirmations: QueryConfirmationStore | None = None,
        query_sessions: CollectionQuerySessionStore | None = None,
    ) -> None:
        self._client = client
        self._confirmations = confirmations
        self._query_confirmations = query_confirmations or QueryConfirmationStore()
        self._query_sessions = query_sessions or CollectionQuerySessionStore()

    def list_collections(
        self,
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
    ) -> dict[str, object]:
        """查询当前用户的首页收藏并创建服务端分页会话。"""

        api_subject_type, api_status = _collection_filter_values(
            subject_type, status
        )
        username = _username(self._client.get_me())
        page = self._read_collection_page(
            username,
            subject_type=api_subject_type,
            status=api_status,
            limit=COLLECTION_DISPLAY_PAGE_SIZE,
            offset=0,
        )
        total = page["total"]
        items = page["collections"]
        returned = len(items)
        query_id, expires_at = self._query_sessions.create_page_session(
            username=username,
            subject_type=subject_type,
            status=status,
            total=total,
            items=items,
        )
        return {
            "user": {"username": username},
            "filters": _collection_filters(subject_type, status),
            "page": {
                "total": total,
                "limit": COLLECTION_DISPLAY_PAGE_SIZE,
                "displayed": returned,
                "returned": returned,
                "has_more": query_id is not None,
                "query_id": query_id,
                "query_expires_at": _timestamp(expires_at),
            },
            "collections": items,
        }

    def count_collections(
        self,
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
    ) -> dict[str, object]:
        """只读取当前用户收藏数量，不扫描全部条目。"""

        api_subject_type, api_status = _collection_filter_values(
            subject_type, status
        )
        username = _username(self._client.get_me())
        page = self._read_collection_page(
            username,
            subject_type=api_subject_type,
            status=api_status,
            limit=1,
            offset=0,
        )
        return {
            "user": {"username": username},
            "filters": _collection_filters(subject_type, status),
            "total": page["total"],
        }

    def continue_collection_query(self, query_id: str) -> dict[str, object]:
        """通过不透明会话 ID 继续查询，达到 100 条前强制确认。"""

        username = _username(self._client.get_me())
        return self._continue_collection_query(query_id.strip(), username)

    def prepare_collection_query(
        self,
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
        operation: CollectionQueryRequestOperation = "list_all",
        min_rating: int | None = None,
        max_rating: int | None = None,
        requested_count: int = COLLECTION_DISPLAY_PAGE_SIZE,
        return_all_matches: bool = False,
    ) -> dict[str, object]:
        """轻量计数并预览完整查询，不执行全量读取。"""

        api_subject_type, api_status = _collection_filter_values(
            subject_type, status
        )
        operation = _query_operation(operation)
        min_rating = _optional_rating(min_rating, "min_rating")
        max_rating = _optional_rating(max_rating, "max_rating")
        if min_rating is not None and max_rating is not None and min_rating > max_rating:
            raise BangumiInputError("min_rating 不能大于 max_rating")
        requested_count = _bounded_int(
            requested_count,
            "requested_count",
            minimum=1,
        )
        if operation == "continue":
            raise BangumiInputError("continue 只能由分页会话生成")
        if operation == "filter" and min_rating is None and max_rating is None:
            raise BangumiInputError("filter 至少需要 min_rating 或 max_rating")
        if operation == "list_all" and (
            min_rating is not None or max_rating is not None
        ):
            raise BangumiInputError("评分条件必须使用 filter 操作")
        if operation != "filter" and return_all_matches:
            raise BangumiInputError("return_all_matches 只支持 filter 操作")
        username = _username(self._client.get_me())
        count_page = self._read_collection_page(
            username,
            subject_type=api_subject_type,
            status=api_status,
            limit=1,
            offset=0,
        )
        candidate_total = count_page["total"]
        confirmation_text = _collection_query_confirmation_text(
            operation=operation,
            subject_type=subject_type,
            status=status,
            candidate_total=candidate_total,
            min_rating=min_rating,
            max_rating=max_rating,
            requested_count=requested_count,
            return_all_matches=return_all_matches,
        )
        pending = self._query_confirmations.prepare(
            PreparedCollectionQuery(
                operation=operation,
                username=username,
                subject_type=subject_type,
                status=status,
                candidate_total=candidate_total,
                confirmation_text=confirmation_text,
                min_rating=min_rating,
                max_rating=max_rating,
                requested_count=requested_count,
                return_all_matches=return_all_matches,
            )
        )
        return _query_confirmation_preview(
            pending.confirmation_id,
            pending.expires_at,
            confirmation_text,
            {
                "user": {"username": username},
                "filters": _collection_filters(subject_type, status),
                "operation": operation,
                "local_filter": _rating_filter(min_rating, max_rating),
                "candidate_total": candidate_total,
                "planned_detail_reads": candidate_total,
                "estimated_list_requests": max(
                    1,
                    ceil(candidate_total / COLLECTION_BULK_PAGE_SIZE),
                ),
                "trigger": {
                    "complete_query": True,
                    "at_least_100_items": candidate_total >= 100,
                },
                "read_only": True,
            },
        )

    def execute_prepared_collection_query(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> dict[str, object]:
        """消费一次性查询确认并执行固定计划。"""

        plan = self._query_confirmations.consume(
            confirmation_id.strip(),
            confirmation_text,
        )
        username = _username(self._client.get_me())
        if username != plan.username:
            raise BangumiInputError("当前 Bangumi 用户已变化，请重新预览")
        if plan.operation == "continue":
            if plan.query_id is None:
                raise QueryStateError("继续查询计划缺少 query_id")
            self._query_sessions.authorize(plan.query_id, username)
            return self._continue_collection_query(plan.query_id, username)

        collections, request_count = self._read_all_collections(plan)
        if plan.operation == "list_all":
            return _complete_query_result(plan, collections, request_count)
        if plan.operation == "analyze":
            return {
                **_complete_query_result(plan, [], request_count),
                "analysis": _collection_analysis(collections),
            }

        matches = [
            item
            for item in collections
            if _rating_matches(item, plan.min_rating, plan.max_rating)
        ]
        if plan.return_all_matches:
            displayed = matches
            query_id = None
            expires_at = None
        else:
            query_id, expires_at, displayed = self._query_sessions.create_result_session(
                username=username,
                subject_type=plan.subject_type,
                status=plan.status,
                source_total=plan.candidate_total,
                items=matches,
                initial_count=plan.requested_count,
            )
        return {
            **_complete_query_result(plan, [], request_count),
            "local_filter": _rating_filter(plan.min_rating, plan.max_rating),
            "matched_total": len(matches),
            "displayed": len(displayed),
            "returned": len(displayed),
            "has_more": query_id is not None,
            "query_id": query_id,
            "query_expires_at": _timestamp(expires_at),
            "collections": displayed,
        }

    def _continue_collection_query(
        self,
        query_id: str,
        username: str,
    ) -> dict[str, object]:
        action = self._query_sessions.next_action(query_id, username)
        if action.kind == "page":
            return _continued_query_result(action)
        if action.kind == "confirm":
            remaining = action.source_total - action.read_count
            confirmation_text = (
                f"确认：允许当前收藏查询从已读 "
                f"{action.read_count} 条继续读取至全部 "
                f"{action.source_total} 条"
            )
            pending = self._query_confirmations.prepare(
                PreparedCollectionQuery(
                    operation="continue",
                    username=username,
                    subject_type=action.subject_type,
                    status=action.status,
                    candidate_total=action.source_total,
                    confirmation_text=confirmation_text,
                    query_id=action.query_id,
                )
            )
            return _query_confirmation_preview(
                pending.confirmation_id,
                pending.expires_at,
                confirmation_text,
                {
                    "user": {"username": username},
                    "filters": _collection_filters(
                        action.subject_type,
                        action.status,
                    ),
                    "query_id": action.query_id,
                    "already_read": action.read_count,
                    "candidate_total": action.source_total,
                    "planned_remaining_reads": remaining,
                    "estimated_list_requests": max(
                        1,
                        ceil(remaining / COLLECTION_BULK_PAGE_SIZE),
                    ),
                    "trigger": {
                        "complete_query": False,
                        "at_least_100_items": True,
                    },
                    "read_only": True,
                },
            )

        api_subject_type, api_status = _collection_filter_values(
            action.subject_type,
            action.status,
        )
        try:
            page = self._read_collection_page(
                username,
                subject_type=api_subject_type,
                status=api_status,
                limit=action.limit,
                offset=action.offset,
            )
            completed = self._query_sessions.finish_fetch(
                action,
                total=page["total"],
                items=page["collections"],
            )
        except BaseException:
            self._query_sessions.cancel_fetch(action)
            raise
        return _continued_query_result(completed)

    def _read_all_collections(
        self,
        plan: PreparedCollectionQuery,
    ) -> tuple[list[dict[str, object]], int]:
        api_subject_type, api_status = _collection_filter_values(
            plan.subject_type,
            plan.status,
        )
        offset = 0
        request_count = 0
        collections: list[dict[str, object]] = []
        subject_ids: set[int] = set()
        while request_count == 0 or offset < plan.candidate_total:
            page = self._read_collection_page(
                plan.username,
                subject_type=api_subject_type,
                status=api_status,
                limit=COLLECTION_BULK_PAGE_SIZE,
                offset=offset,
            )
            request_count += 1
            total = page["total"]
            if total != plan.candidate_total:
                raise BangumiApiError(
                    "Bangumi 收藏总数与已确认查询计划不一致"
                )
            page_items = page["collections"]
            for item in page_items:
                subject_id = _collection_subject_id(item)
                if subject_id in subject_ids:
                    raise BangumiApiError("Bangumi 收藏完整读取出现重复条目")
                subject_ids.add(subject_id)
            collections.extend(page_items)
            offset += len(page_items)
            if plan.candidate_total == 0:
                break
        if len(collections) != plan.candidate_total:
            raise BangumiApiError("Bangumi 收藏完整读取条目数不匹配")
        return collections, request_count

    def _read_collection_page(
        self,
        username: str,
        *,
        subject_type: int | None,
        status: int | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        page = self._client.list_collections(
            username,
            subject_type=subject_type,
            collection_type=status,
            limit=limit,
            offset=offset,
        )
        total = _api_int(page.get("total"), "收藏分页 total", minimum=0)
        response_limit = _api_int(
            page.get("limit"), "收藏分页 limit", minimum=1, maximum=50
        )
        response_offset = _api_int(
            page.get("offset"), "收藏分页 offset", minimum=0
        )
        if response_limit != limit or response_offset != offset:
            raise BangumiApiError("Bangumi 收藏分页参数与请求不匹配")
        raw_items = page.get("data")
        if not isinstance(raw_items, list):
            raise BangumiApiError("Bangumi 收藏分页 data 不是数组")
        collections = [_collection_list_item(item) for item in raw_items]
        if len(collections) > limit:
            raise BangumiApiError("Bangumi 收藏分页返回条目超过 limit")
        if collections and offset + len(collections) > total:
            raise BangumiApiError("Bangumi 收藏分页条目超过 total")
        if not collections and offset < total:
            raise BangumiApiError("Bangumi 收藏分页提前结束")
        return {"total": total, "collections": collections}

    def get_collection_status(self, subject_id: int) -> dict[str, object]:
        """查询作品、收藏状态和逐集动画进度。"""

        subject_id = _positive_int(subject_id, "subject_id")
        subject = self._client.get_subject(subject_id)
        identity = _subject_identity(subject, subject_id)
        username = _username(self._client.get_me())
        collection = self._client.get_collection(username, subject_id)
        result: dict[str, object] = {
            "subject": identity,
            "collected": collection is not None,
            "collection_status": _collection_status(collection),
            "anime_progress": None,
        }
        if identity["type"] == SUBJECT_ANIME and collection is not None:
            episodes = self._client.list_episode_collections(subject_id)
            result["anime_progress"] = _anime_progress(episodes)
        return result

    def prepare_collection_status_update(
        self,
        subject_id: int,
        status: Literal["watching", "completed"],
    ) -> dict[str, object]:
        """只读预览条目状态更新，不执行 Bangumi 写入。"""

        subject_id = _positive_int(subject_id, "subject_id")
        target = STATUS_TARGETS.get(status)
        if target is None:
            raise BangumiInputError("status 只支持 watching 或 completed")
        target_type, target_label = target
        subject = self._client.get_subject(subject_id)
        identity = _subject_identity(subject, subject_id)
        username = _username(self._client.get_me())
        current = self._client.get_collection(username, subject_id)
        current_status = _collection_status(current)
        if current_status is not None and current_status["type"] == target_type:
            return {
                "requires_confirmation": False,
                "no_change": True,
                "subject": identity,
                "current_status": current_status,
                "target_status": {"type": target_type, "label": target_label},
            }

        title = str(identity["title"])
        confirmation_text = f"确认：将《{title}》设置为“{target_label}”"
        pending = self._confirmations.prepare(
            PreparedUpdate(
                kind="collection_status",
                subject_id=subject_id,
                subject_title=title,
                target_label=target_label,
                collection_type=target_type,
                confirmation_text=confirmation_text,
            )
        )
        return _confirmation_preview(
            pending.confirmation_id,
            pending.expires_at,
            confirmation_text,
            {
                "subject": identity,
                "current_status": current_status,
                "target_status": {"type": target_type, "label": target_label},
            },
        )

    def prepare_anime_progress_update(
        self,
        subject_id: int,
        episode_number: int,
    ) -> dict[str, object]:
        """只读预览动画进度更新，按章节 ID 解析第 1 至 N 集。"""

        subject_id = _positive_int(subject_id, "subject_id")
        episode_number = _positive_int(episode_number, "episode_number")
        subject = self._client.get_subject(subject_id)
        identity = _subject_identity(subject, subject_id)
        if identity["type"] != SUBJECT_ANIME:
            raise BangumiInputError("逐集进度只支持动画条目")
        username = _username(self._client.get_me())
        if self._client.get_collection(username, subject_id) is None:
            raise BangumiInputError("条目尚未收藏，请先明确设置为“在看”或“看过”")

        episodes = self._client.list_episode_collections(subject_id)
        target = _episode_by_number(episodes, episode_number)
        to_mark = _episodes_to_mark(episodes, episode_number)
        if not to_mark:
            return {
                "requires_confirmation": False,
                "no_change": True,
                "subject": identity,
                "target_episode": _episode_summary(target),
                "anime_progress": _anime_progress(episodes),
            }

        title = str(identity["title"])
        confirmation_text = (
            f"确认：将《{title}》动画进度更新到第 {episode_number} 集"
            "（逐集标记为看过）"
        )
        pending = self._confirmations.prepare(
            PreparedUpdate(
                kind="anime_progress",
                subject_id=subject_id,
                subject_title=title,
                target_label=f"第 {episode_number} 集",
                episode_number=episode_number,
                target_episode_id=_episode_id(target),
                confirmation_text=confirmation_text,
            )
        )
        return _confirmation_preview(
            pending.confirmation_id,
            pending.expires_at,
            confirmation_text,
            {
                "subject": identity,
                "target_episode": _episode_summary(target),
                "episodes_to_mark": [
                    _episode_number(item) for item in to_mark
                ],
                "episode_count": len(to_mark),
                "current_progress": _anime_progress(episodes),
            },
        )

    def commit_prepared_update(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> dict[str, object]:
        """消费一次性确认并执行唯一一次 Bangumi 写操作。"""

        operation = self._confirmations.consume(
            confirmation_id.strip(),
            confirmation_text,
        )
        subject = self._client.get_subject(operation.subject_id)
        identity = _subject_identity(subject, operation.subject_id)
        if identity["title"] != operation.subject_title:
            raise BangumiInputError("作品显示名称已经变化，请重新预览并确认")

        if operation.kind == "collection_status":
            if operation.collection_type is None:
                raise RuntimeError("收藏状态确认记录损坏")
            self._client.set_collection_type(
                operation.subject_id,
                operation.collection_type,
            )
            return {
                "updated": True,
                "subject": identity,
                "collection_status": {
                    "type": operation.collection_type,
                    "label": operation.target_label,
                },
            }

        if operation.episode_number is None or operation.target_episode_id is None:
            raise RuntimeError("动画进度确认记录损坏")
        username = _username(self._client.get_me())
        if self._client.get_collection(username, operation.subject_id) is None:
            raise BangumiInputError("条目已不在收藏中，请重新预览")
        episodes = self._client.list_episode_collections(operation.subject_id)
        target = _episode_by_number(episodes, operation.episode_number)
        if _episode_id(target) != operation.target_episode_id:
            raise BangumiInputError("目标章节已经变化，请重新预览并确认")
        to_mark = _episodes_to_mark(episodes, operation.episode_number)
        if not to_mark:
            return {
                "updated": False,
                "no_change": True,
                "subject": identity,
                "anime_progress": _anime_progress(episodes),
            }
        self._client.set_episode_collections(
            operation.subject_id,
            [_episode_id(item) for item in to_mark],
            collection_type=EPISODE_WATCHED,
        )
        return {
            "updated": True,
            "subject": identity,
            "target_episode": _episode_summary(target),
            "marked_episode_numbers": [
                _episode_number(item) for item in to_mark
            ],
        }


def _confirmation_preview(
    confirmation_id: str,
    expires_at: float,
    confirmation_text: str,
    target: dict[str, object],
) -> dict[str, object]:
    return {
        "requires_confirmation": True,
        "confirmation_id": confirmation_id,
        "expires_at": datetime.fromtimestamp(
            expires_at,
            tz=timezone.utc,
        ).isoformat(),
        "confirmation_text": confirmation_text,
        "target": target,
        "instruction": "显示 target 和 confirmation_text 后结束本轮，等待用户逐字确认。",
    }


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BangumiInputError(f"{label} 必须是正整数")
    return value


def _bounded_int(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BangumiInputError(f"{label} 必须是整数")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = (
            f"{minimum} 至 {maximum}"
            if maximum is not None
            else f"至少 {minimum}"
        )
        raise BangumiInputError(f"{label} 必须为 {bounds}")
    return value


def _filter_value(
    value: object,
    mapping: dict[str, int | None],
    label: str,
) -> int | None:
    if not isinstance(value, str) or value not in mapping:
        allowed = "、".join(mapping)
        raise BangumiInputError(f"{label} 只支持 {allowed}")
    return mapping[value]


def _collection_filter_values(
    subject_type: SubjectTypeFilter,
    status: CollectionStatusFilter,
) -> tuple[int | None, int | None]:
    return (
        _filter_value(subject_type, SUBJECT_TYPE_FILTERS, "subject_type"),
        _filter_value(status, COLLECTION_STATUS_FILTERS, "status"),
    )


def _collection_filters(
    subject_type: SubjectTypeFilter,
    status: CollectionStatusFilter,
) -> dict[str, str]:
    return {"subject_type": subject_type, "status": status}


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _query_operation(value: object) -> CollectionQueryOperation:
    if value not in {"list_all", "filter", "analyze", "continue"}:
        raise BangumiInputError(
            "operation 只支持 list_all、filter 或 analyze"
        )
    return value  # type: ignore[return-value]


def _optional_rating(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, label, minimum=0, maximum=10)


def _rating_filter(
    min_rating: int | None,
    max_rating: int | None,
) -> dict[str, int | None] | None:
    if min_rating is None and max_rating is None:
        return None
    return {"min_rating": min_rating, "max_rating": max_rating}


def _collection_query_confirmation_text(
    *,
    operation: CollectionQueryOperation,
    subject_type: str,
    status: str,
    candidate_total: int,
    min_rating: int | None,
    max_rating: int | None,
    requested_count: int,
    return_all_matches: bool,
) -> str:
    scope = f"subject_type={subject_type}, status={status}"
    if operation == "list_all":
        purpose = "全部列出"
    elif operation == "analyze":
        purpose = "完整统计分析"
    else:
        bounds: list[str] = []
        if min_rating is not None:
            bounds.append(f"评分至少 {min_rating}")
        if max_rating is not None:
            bounds.append(f"评分至多 {max_rating}")
        output = "全部匹配结果" if return_all_matches else f"前 {requested_count} 条匹配结果"
        purpose = f"筛选{' 且 '.join(bounds)}并返回{output}"
    return (
        f"确认：完整读取当前用户的 {candidate_total} 条候选收藏"
        f"（{scope}），用于{purpose}"
    )


def _query_confirmation_preview(
    confirmation_id: str,
    expires_at: float,
    confirmation_text: str,
    target: dict[str, object],
) -> dict[str, object]:
    return {
        "requires_confirmation": True,
        "confirmation_id": confirmation_id,
        "expires_at": _timestamp(expires_at),
        "confirmation_text": confirmation_text,
        "target": target,
        "instruction": (
            "显示 target 和 confirmation_text 后结束本轮；"
            "只能在用户下一条消息逐字确认后执行查询。"
        ),
    }


def _continued_query_result(action: ContinueAction) -> dict[str, object]:
    items = list(action.items)
    has_more = action.displayed_count < action.total
    return {
        "user": {"username": action.username},
        "filters": _collection_filters(action.subject_type, action.status),
        "page": {
            "total": action.total,
            "limit": COLLECTION_DISPLAY_PAGE_SIZE,
            "displayed": action.displayed_count,
            "returned": len(items),
            "has_more": has_more,
            "query_id": action.query_id if has_more else None,
        },
        "collections": items,
    }


def _complete_query_result(
    plan: PreparedCollectionQuery,
    collections: list[dict[str, object]],
    request_count: int,
) -> dict[str, object]:
    return {
        "user": {"username": plan.username},
        "filters": _collection_filters(plan.subject_type, plan.status),
        "operation": plan.operation,
        "complete": True,
        "candidate_total": plan.candidate_total,
        "scanned": plan.candidate_total,
        "api_page_size": COLLECTION_BULK_PAGE_SIZE,
        "request_count": request_count,
        "returned": len(collections),
        "collections": collections,
    }


def _collection_subject_id(item: dict[str, object]) -> int:
    subject = item.get("subject")
    if not isinstance(subject, dict):
        raise BangumiApiError("Bangumi 收藏条目缺少 subject")
    subject_id = subject.get("id")
    if not isinstance(subject_id, int) or isinstance(subject_id, bool):
        raise BangumiApiError("Bangumi 收藏条目 subject id 无效")
    return subject_id


def _collection_rating(item: dict[str, object]) -> int:
    collection = item.get("collection")
    if not isinstance(collection, dict):
        raise BangumiApiError("Bangumi 收藏条目缺少 collection")
    rating = collection.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool):
        raise BangumiApiError("Bangumi 收藏条目 rating 无效")
    return rating


def _rating_matches(
    item: dict[str, object],
    min_rating: int | None,
    max_rating: int | None,
) -> bool:
    rating = _collection_rating(item)
    return (
        (min_rating is None or rating >= min_rating)
        and (max_rating is None or rating <= max_rating)
    )


def _collection_analysis(
    items: list[dict[str, object]],
) -> dict[str, object]:
    rating_distribution = {str(value): 0 for value in range(11)}
    status_distribution: dict[str, int] = {}
    subject_type_distribution: dict[str, int] = {}
    rated_values: list[int] = []
    for item in items:
        rating = _collection_rating(item)
        rating_distribution[str(rating)] += 1
        if rating > 0:
            rated_values.append(rating)
        collection = item["collection"]
        subject = item["subject"]
        if not isinstance(collection, dict) or not isinstance(subject, dict):
            raise BangumiApiError("Bangumi 收藏分析条目无效")
        status = collection.get("status")
        subject_type = subject.get("type_value")
        if not isinstance(status, str) or not isinstance(subject_type, str):
            raise BangumiApiError("Bangumi 收藏分析分类无效")
        status_distribution[status] = status_distribution.get(status, 0) + 1
        subject_type_distribution[subject_type] = (
            subject_type_distribution.get(subject_type, 0) + 1
        )
    return {
        "total": len(items),
        "rated_count": len(rated_values),
        "unrated_count": len(items) - len(rated_values),
        "average_rating": (
            round(sum(rated_values) / len(rated_values), 2)
            if rated_values
            else None
        ),
        "rating_distribution": rating_distribution,
        "status_distribution": status_distribution,
        "subject_type_distribution": subject_type_distribution,
    }


def _subject_identity(subject: dict[str, Any], subject_id: int) -> dict[str, object]:
    actual_id = subject.get("id")
    if actual_id != subject_id:
        raise BangumiApiError("Bangumi 返回的条目 ID 不匹配")
    subject_type = subject.get("type")
    if isinstance(subject_type, bool) or not isinstance(subject_type, int):
        raise BangumiApiError("Bangumi 返回的条目类型无效")
    name_cn = subject.get("name_cn")
    name = subject.get("name")
    raw_title = name_cn if isinstance(name_cn, str) and name_cn.strip() else name
    if not isinstance(raw_title, str) or not raw_title.strip():
        raise BangumiApiError("Bangumi 返回的作品名为空")
    title = " ".join(raw_title.split())[:160]
    return {
        "id": subject_id,
        "title": title,
        "type": subject_type,
        "type_label": SUBJECT_TYPES.get(subject_type, "未知"),
    }


def _username(payload: dict[str, Any]) -> str:
    username = payload.get("username")
    if not isinstance(username, str) or not username.strip():
        raise BangumiApiError("Bangumi 当前用户缺少 username")
    return username.strip()


def _collection_status(collection: dict[str, Any] | None) -> dict[str, object] | None:
    if collection is None:
        return None
    collection_type = collection.get("type")
    if isinstance(collection_type, bool) or not isinstance(collection_type, int):
        raise BangumiApiError("Bangumi 收藏状态无效")
    return {
        "type": collection_type,
        "label": COLLECTION_TYPES.get(collection_type, "未知"),
    }


def _collection_list_item(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BangumiApiError("Bangumi 收藏分页条目不是对象")
    subject_id = _api_int(value.get("subject_id"), "收藏 subject_id", minimum=1)
    subject_type = _api_enum(
        value.get("subject_type"),
        SUBJECT_TYPE_VALUES,
        "收藏 subject_type",
    )
    collection_type = _api_enum(
        value.get("type"),
        COLLECTION_STATUS_VALUES,
        "收藏 type",
    )
    rating = _api_int(value.get("rate"), "收藏 rate", minimum=0, maximum=10)
    episode_progress = _api_int(
        value.get("ep_status"),
        "收藏 ep_status",
        minimum=0,
    )
    volume_progress = _api_int(
        value.get("vol_status"),
        "收藏 vol_status",
        minimum=0,
    )
    private = value.get("private")
    if not isinstance(private, bool):
        raise BangumiApiError("Bangumi 收藏 private 不是布尔值")
    updated_at = value.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise BangumiApiError("Bangumi 收藏 updated_at 无效")

    title: str | None = None
    episodes: int | None = None
    volumes: int | None = None
    subject = value.get("subject")
    if subject is not None:
        if not isinstance(subject, dict):
            raise BangumiApiError("Bangumi 收藏 subject 不是对象")
        if subject.get("id") != subject_id:
            raise BangumiApiError("Bangumi 收藏 subject ID 不匹配")
        if subject.get("type") != subject_type:
            raise BangumiApiError("Bangumi 收藏 subject 类型不匹配")
        title = _optional_subject_title(subject)
        episodes = _api_int(subject.get("eps"), "收藏 subject eps", minimum=0)
        volumes = _api_int(
            subject.get("volumes"),
            "收藏 subject volumes",
            minimum=0,
        )

    return {
        "subject": {
            "id": subject_id,
            "title": title,
            "type": subject_type,
            "type_value": SUBJECT_TYPE_VALUES[subject_type],
            "type_label": SUBJECT_TYPES[subject_type],
            "episodes": episodes,
            "volumes": volumes,
        },
        "collection": {
            "type": collection_type,
            "status": COLLECTION_STATUS_VALUES[collection_type],
            "status_label": COLLECTION_TYPES[collection_type],
            "rating": rating,
            "reported_episode_progress": episode_progress,
            "reported_volume_progress": volume_progress,
            "private": private,
            "updated_at": updated_at.strip(),
        },
    }


def _api_int(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        return _bounded_int(
            value,
            label,
            minimum=minimum,
            maximum=maximum,
        )
    except BangumiInputError as error:
        raise BangumiApiError(str(error)) from None


def _api_enum(
    value: object,
    mapping: dict[int, str],
    label: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in mapping:
        raise BangumiApiError(f"Bangumi {label} 无效")
    return value


def _optional_subject_title(subject: dict[str, Any]) -> str | None:
    name_cn = subject.get("name_cn")
    name = subject.get("name")
    if name_cn is not None and not isinstance(name_cn, str):
        raise BangumiApiError("Bangumi 收藏 subject name_cn 无效")
    if name is not None and not isinstance(name, str):
        raise BangumiApiError("Bangumi 收藏 subject name 无效")
    raw_title = name_cn if isinstance(name_cn, str) and name_cn.strip() else name
    if not isinstance(raw_title, str) or not raw_title.strip():
        return None
    return " ".join(raw_title.split())[:160]


def _episode_payload(item: dict[str, Any]) -> dict[str, Any]:
    episode = item.get("episode")
    if not isinstance(episode, dict):
        raise BangumiApiError("Bangumi 章节收藏缺少 episode")
    return episode


def _episode_id(item: dict[str, Any]) -> int:
    value = _episode_payload(item).get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BangumiApiError("Bangumi 章节 ID 无效")
    return value


def _episode_number(item: dict[str, Any]) -> int:
    value = _episode_payload(item).get("sort")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise BangumiApiError("Bangumi 章节序号无效")
    number = int(value)
    if number <= 0 or float(value) != number:
        raise BangumiApiError("Bangumi 本篇章节序号必须是正整数")
    return number


def _episode_collection_type(item: dict[str, Any]) -> int:
    value = item.get("type")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BangumiApiError("Bangumi 章节收藏状态无效")
    return value


def _episode_by_number(
    episodes: list[dict[str, Any]],
    episode_number: int,
) -> dict[str, Any]:
    matches = [item for item in episodes if _episode_number(item) == episode_number]
    if len(matches) != 1:
        raise BangumiInputError(
            f"无法唯一定位动画第 {episode_number} 集，找到 {len(matches)} 条"
        )
    return matches[0]


def _episodes_to_mark(
    episodes: list[dict[str, Any]],
    episode_number: int,
) -> list[dict[str, Any]]:
    selected = [
        item
        for item in episodes
        if _episode_number(item) <= episode_number
        and _episode_collection_type(item) != EPISODE_WATCHED
    ]
    return sorted(selected, key=_episode_number)


def _episode_summary(item: dict[str, Any]) -> dict[str, object]:
    episode = _episode_payload(item)
    name_cn = episode.get("name_cn")
    name = episode.get("name")
    raw_title = name_cn if isinstance(name_cn, str) and name_cn.strip() else name
    title = " ".join(raw_title.split())[:160] if isinstance(raw_title, str) else ""
    return {
        "id": _episode_id(item),
        "number": _episode_number(item),
        "title": title,
        "collection_type": _episode_collection_type(item),
    }


def _anime_progress(episodes: list[dict[str, Any]]) -> dict[str, object]:
    by_number: dict[int, int] = {}
    for item in episodes:
        number = _episode_number(item)
        if number in by_number:
            raise BangumiApiError(f"Bangumi 本篇章节序号重复: {number}")
        by_number[number] = _episode_collection_type(item)
    watched = sorted(
        number
        for number, collection_type in by_number.items()
        if collection_type == EPISODE_WATCHED
    )
    watched_through = 0
    while by_number.get(watched_through + 1) == EPISODE_WATCHED:
        watched_through += 1
    highest = watched[-1] if watched else 0
    gaps = [
        number
        for number in range(1, highest)
        if by_number.get(number) != EPISODE_WATCHED
    ]
    return {
        "highest_watched_episode": highest,
        "watched_through_episode": watched_through,
        "watched_episode_count": len(watched),
        "unwatched_before_highest": gaps,
        "main_episode_count": len(by_number),
    }
