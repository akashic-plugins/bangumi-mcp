from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .client import BangumiClient
from .config import load_runtime_config
from .confirmation import ConfirmationStore
from .query import (
    CollectionQueryRequestOperation,
    CollectionQuerySessionStore,
    QueryConfirmationStore,
)
from .service import BangumiService, CollectionStatusFilter, SubjectTypeFilter


def create_mcp_server(data_dir: Path) -> FastMCP:
    confirmations = ConfirmationStore()
    query_confirmations = QueryConfirmationStore()
    query_sessions = CollectionQuerySessionStore()
    mcp = FastMCP("bangumi")

    def service() -> BangumiService:
        config = load_runtime_config(data_dir)
        return BangumiService(
            BangumiClient(config),
            confirmations,
            query_confirmations,
            query_sessions,
        )

    @mcp.tool()
    def get_collection_status(subject_id: int) -> str:
        """查询指定 Bangumi 条目的收藏状态及动画逐集观看进度。"""

        return _json(service().get_collection_status(subject_id))

    @mcp.tool()
    def list_collections(
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
    ) -> str:
        """创建普通收藏查询会话；只读，首页固定 10 条。"""

        return _json(
            service().list_collections(
                subject_type=subject_type,
                status=status,
            )
        )

    @mcp.tool()
    def continue_collection_query(query_id: str) -> str:
        """通过不透明 query_id 继续 10 条展示页；达到 100 条前要求确认。"""

        return _json(service().continue_collection_query(query_id))

    @mcp.tool()
    def count_collections(
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
    ) -> str:
        """只查询当前 Token 用户匹配收藏的总数；只读。"""

        return _json(
            service().count_collections(
                subject_type=subject_type,
                status=status,
            )
        )

    @mcp.tool()
    def prepare_collection_query(
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
        operation: CollectionQueryRequestOperation = "list_all",
        min_rating: int | None = None,
        max_rating: int | None = None,
        requested_count: int = 10,
        return_all_matches: bool = False,
    ) -> str:
        """预览完整或大量收藏查询；只计数，绝不执行全量读取。"""

        return _json(
            service().prepare_collection_query(
                subject_type=subject_type,
                status=status,
                operation=operation,
                min_rating=min_rating,
                max_rating=max_rating,
                requested_count=requested_count,
                return_all_matches=return_all_matches,
            )
        )

    @mcp.tool()
    def execute_prepared_collection_query(
        confirmation_id: str,
        confirmation_text: str,
    ) -> str:
        """执行已确认的固定只读查询计划；禁止与 prepare 同轮调用。"""

        return _json(
            service().execute_prepared_collection_query(
                confirmation_id,
                confirmation_text,
            )
        )

    @mcp.tool()
    def prepare_collection_status_update(
        subject_id: int,
        status: Literal["watching", "completed"],
    ) -> str:
        """预览把条目设为在看或看过；此工具绝不执行 Bangumi 写入。"""

        return _json(service().prepare_collection_status_update(subject_id, status))

    @mcp.tool()
    def prepare_anime_progress_update(
        subject_id: int,
        episode_number: int,
    ) -> str:
        """预览把动画第 1 至 N 集逐集标为看过；此工具绝不写入。"""

        return _json(
            service().prepare_anime_progress_update(subject_id, episode_number)
        )

    @mcp.tool()
    def commit_prepared_update(
        confirmation_id: str,
        confirmation_text: str,
    ) -> str:
        """执行已预览写入。只能在用户最新消息逐字确认后调用，禁止与 prepare 同轮调用。"""

        return _json(
            service().commit_prepared_update(
                confirmation_id,
                confirmation_text,
            )
        )

    return mcp


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
