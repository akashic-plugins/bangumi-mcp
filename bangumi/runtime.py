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
from .anime_push_loop import AnimeUpdateLoop, PluginTimersPort
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
    from .anime_updates import AnimeUpdateCoordinator
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
    timers: PluginTimersPort | None = None
    ctx: object | None = None
    _service: BangumiService | None = field(default=None, init=False, repr=False)
    _store: object | None = field(default=None, init=False, repr=False)
    _lease: object | None = field(default=None, init=False, repr=False)
    _background_lease: object | None = field(default=None, init=False, repr=False)
    _loop: AnimeUpdateLoop | None = field(default=None, init=False, repr=False)

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
        """只读返回到期且尚未 ACK 的放送事件；未来接主动投递时复用。"""

        return self._anime_store().fetch_pending(offset, limit)  # type: ignore[attr-defined]

    def acknowledge_anime_update_alerts(self, event_ids: list[str]) -> object:
        """按 event_id 确认放送事件；未来由投递方 ACK，现在只供显式清理。"""

        count = self._anime_store().acknowledge(  # type: ignore[attr-defined]
            event_ids,
            datetime.now(timezone.utc),
        )
        return {"acked": count}

    # ---- 后台刷新（只抓取，不投递）----

    @property
    def background_refresh_enabled(self) -> bool:
        """只有启用 anime_push 且配置了 AniList Token 才启动刷新。"""

        push = self.settings.anime_push
        return push.enabled and push.anilist_token is not None

    def anime_update_loop(self) -> AnimeUpdateLoop:
        """返回本次 apply 唯一的后台刷新循环。"""

        if self._loop is None:
            self._loop = AnimeUpdateLoop(
                coordinator=self._coordinator(),
                timers=self.timers,
            )
        return self._loop

    async def start_background_refresh(self) -> None:
        """为刷新循环开一份长期租约并启动它；同一代内重复调用是幂等的。

        循环是 generation 级资源：本代停止后不原地重启，换代由新的
        ``apply`` 重新构造运行时。
        """

        if not self.background_refresh_enabled:
            return
        if self._background_lease is None:
            self._background_lease = await self._enter_lease()
        await self.anime_update_loop().start()

    async def stop_background_refresh(self) -> None:
        """停止刷新循环并关闭它的长期租约。"""

        loop = self._loop
        if loop is not None:
            await loop.stop()
        lease = self._background_lease
        self._background_lease = None
        if lease is not None:
            closer = getattr(lease, "aclose", None)
            if callable(closer):
                await closer()

    def _coordinator(self) -> AnimeUpdateCoordinator:
        from .anilist import AniListClient
        from .anime_updates import AnimeUpdateCoordinator

        return AnimeUpdateCoordinator(
            self._anime_store(),  # type: ignore[arg-type]
            self._bangumi(),
            AniListClient(
                user_agent=self.settings.user_agent,
                token_provider=lambda: self._token("anilist_token"),
            ),
            self.settings.anime_push,
        )

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
        if self._background_lease is not None:
            return self._background_lease
        return await self._enter_lease()

    async def _enter_lease(self) -> object | None:
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
        lease = self._lease if self._lease is not None else self._background_lease
        if lease is None:
            raise ToolExecutionError("Bangumi 凭据租约不可用")
        lease_port: CredentialLease = lease  # type: ignore[assignment]
        value = lease_port.credential(self._credential_ref(alias))
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
        """放送提醒设置；后台刷新循环与投递适配层共用。"""

        return self.settings.anime_push


def _error_text(error: BaseException) -> str:
    text = str(error).strip()
    return text or error.__class__.__name__


def build_runtime(
    *,
    settings: BangumiSettings,
    data_dir: Path,
    credentials: CredentialProvider | None,
    timers: PluginTimersPort | None,
    ctx: object,
) -> BangumiRuntime:
    """构造一次 apply 的运行时；凭据只在调用期间解析。"""

    return BangumiRuntime(
        settings=settings,
        data_dir=data_dir,
        credentials=credentials,
        timers=timers,
        ctx=ctx,
    )


__all__ = [
    "BangumiRuntime",
    "CredentialAlias",
    "CredentialLease",
    "CredentialProvider",
    "build_runtime",
]
