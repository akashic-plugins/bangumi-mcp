"""插件运行时：把固定配置、凭据租约与工具调用接到领域实现。

工具调用是异步的：每次调用先打开一次宿主凭据短租约，把同步 HTTP/SQLite 工作
放到线程执行，最后关闭租约。凭据只在调用期间存在，不长期持有明文；candidate
校验期若宿主拒绝解析正式凭据，调用会返回明确错误而不是静默使用空 Token。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from ._host_contract import CredentialRef, ToolResult
from .confirmation import ConfirmationStore
from .query import CollectionQuerySessionStore, QueryConfirmationStore
from .service import (
    BangumiService,
    CollectionStatusFilter,
    SubjectTypeFilter,
)
from .settings import BangumiRuntimeConfig, BangumiSettings
from .tools import ToolExecutionError, tool_error, tool_success

if TYPE_CHECKING:
    from .client import BangumiClient
    from .query import CollectionQueryRequestOperation
    from .settings import AnimePushSettings

CredentialAlias = Literal["access_token", "anilist_token"]
CollectionTargetStatus = Literal["watching", "completed"]

T = TypeVar("T")


class CredentialProvider(Protocol):
    """宿主凭据短租约：只解析本插件固定输入里出现的引用。"""

    def open(
        self,
        ctx: object,
        refs: Mapping[str, CredentialRef],
    ) -> AbstractAsyncContextManager[object]: ...


class CredentialLease(Protocol):
    def credential(self, ref: CredentialRef) -> str: ...


@dataclass
class BangumiRuntime:
    """一次 ``apply`` 拥有的全部运行状态。"""

    settings: BangumiSettings
    data_dir: Path
    credentials: CredentialProvider | None = None
    ctx: object | None = None
    _service: BangumiService | None = field(default=None, init=False, repr=False)
    _store: object | None = field(default=None, init=False, repr=False)
    _lease: object | None = field(default=None, init=False, repr=False)

    # ---- 发现合同 ----

    def catalog(self) -> object:
        """返回绑定到本运行时的工具目录。"""

        from .tools import build_tool_catalog

        return build_tool_catalog(invoke=self.invoke)

    # ---- 领域调用（同步实现，由 invoke 放到线程执行）----

    def _bangumi(self) -> BangumiService:
        if self._service is None:
            from .client import BangumiClient

            client: BangumiClient = BangumiClient(
                BangumiRuntimeConfig(
                    token_provider=lambda: self._token("access_token"),
                    user_agent=self.settings.user_agent,
                )
            )
            self._service = BangumiService(
                client,
                ConfirmationStore(),
                QueryConfirmationStore(),
                CollectionQuerySessionStore(),
            )
        return self._service

    def list_collections(
        self,
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
    ) -> object:
        return self._bangumi().list_collections(subject_type, status)

    def count_collections(
        self,
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
    ) -> object:
        return self._bangumi().count_collections(subject_type, status)

    def continue_collection_query(self, query_id: str) -> object:
        return self._bangumi().continue_collection_query(query_id)

    def get_collection_status(self, subject_id: int) -> object:
        return self._bangumi().get_collection_status(subject_id)

    def prepare_collection_query(
        self,
        subject_type: SubjectTypeFilter = "all",
        status: CollectionStatusFilter = "all",
        operation: CollectionQueryRequestOperation = "list_all",
        min_rating: int | None = None,
        max_rating: int | None = None,
        requested_count: int = 10,
        return_all_matches: bool = False,
    ) -> object:
        return self._bangumi().prepare_collection_query(
            subject_type=subject_type,
            status=status,
            operation=operation,
            min_rating=min_rating,
            max_rating=max_rating,
            requested_count=requested_count,
            return_all_matches=return_all_matches,
        )

    def execute_prepared_collection_query(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> object:
        return self._bangumi().execute_prepared_collection_query(
            confirmation_id,
            confirmation_text,
        )

    def prepare_collection_status_update(
        self,
        subject_id: int,
        status: CollectionTargetStatus,
    ) -> object:
        return self._bangumi().prepare_collection_status_update(
            subject_id,
            status,
        )

    def prepare_anime_progress_update(
        self,
        subject_id: int,
        episode_number: int,
    ) -> object:
        return self._bangumi().prepare_anime_progress_update(
            subject_id,
            episode_number,
        )

    def commit_prepared_update(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> object:
        return self._bangumi().commit_prepared_update(
            confirmation_id,
            confirmation_text,
        )

    def _anime_store(self) -> object:
        if self._store is None:
            from .anime_updates import AnimeUpdateStore

            self._store = AnimeUpdateStore(self.data_dir / "anime_updates.db")
        return self._store

    def fetch_pending_alerts(self, offset: int = 0, limit: int = 50) -> object:
        return self._anime_store().fetch_pending(offset, limit)  # type: ignore[attr-defined]

    def acknowledge_anime_update_alerts(self, event_ids: list[str]) -> object:
        count = self._anime_store().acknowledge(  # type: ignore[attr-defined]
            event_ids,
            datetime.now(timezone.utc),
        )
        return {"acked": count}

    # ---- 工具入口 ----

    async def invoke(
        self,
        method: str,
        *args: object,
        **kwargs: object,
    ) -> ToolResult:
        """在凭据租约内执行一次领域调用，并把错误转成工具结果。"""

        def work() -> object:
            target = getattr(self, method, None)
            if not callable(target):
                raise ToolExecutionError(f"服务缺少实现: {method}")
            return target(*args, **kwargs)

        try:
            payload = await self._run_in_lease(work)
        except ToolExecutionError as error:
            return tool_error(str(error))
        except Exception as error:  # noqa: BLE001 - 领域错误统一转成工具错误
            return tool_error(_error_text(error))
        return tool_success(payload)

    async def _run_in_lease(self, work: Callable[[], T]) -> T:
        lease = await self._open_lease()
        if lease is None:
            return await asyncio.to_thread(work)
        self._lease = lease
        try:
            return await asyncio.to_thread(work)
        finally:
            self._lease = None
            closer = getattr(lease, "aclose", None)
            if callable(closer):
                await closer()

    async def _open_lease(self) -> object | None:
        if self.credentials is None or self.ctx is None:
            return None
        refs: dict[str, CredentialRef] = {
            "access_token": self.settings.access_token
        }
        anilist = self.settings.anime_push.anilist_token
        if anilist is not None:
            refs["anilist_token"] = anilist
        manager = self.credentials.open(self.ctx, refs)
        return await manager.__aenter__()

    def _token(self, alias: CredentialAlias) -> str:
        if self._lease is None:
            raise ToolExecutionError("Bangumi 凭据租约不可用")
        lease: CredentialLease = self._lease  # type: ignore[assignment]
        value = lease.credential(self._credential_ref(alias))
        if not isinstance(value, str) or not value.strip():
            raise ToolExecutionError("凭据内容为空")
        return value.strip()

    def _credential_ref(self, alias: CredentialAlias) -> CredentialRef:
        if alias == "access_token":
            return self.settings.access_token
        ref = self.settings.anime_push.anilist_token
        if ref is None:
            raise ToolExecutionError("未配置 AniList Token")
        return ref

    @property
    def anime_push_settings(self) -> AnimePushSettings:
        """放送提醒设置；Phase 3 的后台刷新循环使用。"""

        return self.settings.anime_push


def _error_text(error: BaseException) -> str:
    text = str(error).strip()
    return text or error.__class__.__name__


def build_runtime(
    *,
    settings: BangumiSettings,
    data_dir: Path,
    credentials: CredentialProvider | None,
    ctx: object,
) -> BangumiRuntime:
    """构造一次 apply 的运行时；凭据只在调用期间解析。"""

    return BangumiRuntime(
        settings=settings,
        data_dir=data_dir,
        credentials=credentials,
        ctx=ctx,
    )


__all__ = [
    "BangumiRuntime",
    "CredentialAlias",
    "CredentialLease",
    "CredentialProvider",
    "build_runtime",
]
