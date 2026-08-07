from __future__ import annotations

import asyncio
import json
import warnings
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .anilist import AniListClient
from .anime_updates import AnimeUpdateCoordinator, AnimeUpdateStore
from .client import BangumiClient
from .config import BangumiRuntimeConfig, load_runtime_config
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
    runtime_config: BangumiRuntimeConfig | None = None
    coordinator: AnimeUpdateCoordinator | None = None
    store = AnimeUpdateStore(data_dir / "anime_updates.db")
    if (data_dir / "config.local.toml").exists():
        runtime_config = load_runtime_config(data_dir)
        if runtime_config.anime_push.enabled:
            store.initialize()
            coordinator = AnimeUpdateCoordinator(
                store,
                BangumiClient(runtime_config),
                AniListClient(
                    user_agent=runtime_config.user_agent,
                    token=runtime_config.anime_push.anilist_token,
                ),
                runtime_config.anime_push,
            )

    @asynccontextmanager
    async def lifespan(_):
        task: asyncio.Task[None] | None = None
        if coordinator is not None:
            task = asyncio.create_task(
                coordinator.run(),
                name="bangumi-anime-update-refresh",
            )
        try:
            yield {"anime_update_coordinator": coordinator}
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    # MCP 1.26 leaves its generic lifespan Settings field unresolved.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Field 'lifespan' has an incomplete definition.*",
        )
        if coordinator is None:
            mcp = FastMCP("bangumi")
        else:
            mcp = FastMCP("bangumi", lifespan=lifespan)

    def service() -> BangumiService:
        config = load_runtime_config(data_dir)
        return BangumiService(
            BangumiClient(config),
            confirmations,
            query_confirmations,
            query_sessions,
        )

    def anime_store() -> AnimeUpdateStore:
        if runtime_config is None or not runtime_config.anime_push.enabled:
            raise RuntimeError("anime_push 未启用")
        return store

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

    @mcp.tool()
    def get_anime_update_alerts(offset: int = 0, limit: int = 50) -> str:
        """只读返回本地已到期且尚未 ACK 的放送提醒快照。"""

        return _json(anime_store().fetch_pending(offset, limit))

    @mcp.tool()
    def acknowledge_anime_update_alerts(
        event_ids: list[str],
        feedback: str | None = None,
    ) -> str:
        """在真实外部投递成功后，按 event_id 原子确认放送提醒。"""

        del feedback
        count = anime_store().acknowledge(
            event_ids,
            datetime.now(timezone.utc),
        )
        return _json({"acked": count})

    return mcp


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
