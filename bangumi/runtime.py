"""插件运行时：把固定配置、凭据租约与工具调用接到领域实现。

工具调用是异步的：每次调用先打开一次宿主凭据短租约，把同步 HTTP/SQLite 工作
放到线程执行，最后关闭租约。凭据只在调用期间存在，不长期持有明文；candidate
校验期若宿主拒绝解析正式凭据，调用会返回明确错误而不是静默使用空 Token。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from ._host_contract import (
    ALERT_SOURCE,
    CONTEXT_SOURCE,
    CredentialRef,
    PluginContext,
)
from .alert_source import SOURCE_ID, AlertBinding, BangumiAlertBridge
from .anime_push_loop import AnimeUpdateLoop, PluginTimersPort
from .confirmation import ConfirmationStore
from .query import CollectionQuerySessionStore, QueryConfirmationStore
from .service import (
    BangumiService,
    CollectionStatusFilter,
    SubjectTypeFilter,
)
from .settings import BangumiRuntimeConfig, BangumiSettings
from .tools import ToolExecutionError, ToolResultValue, tool_error, tool_success

if TYPE_CHECKING:
    from .anime_updates import AnimeUpdateCoordinator
    from .client import BangumiClient
    from .query import CollectionQueryRequestOperation
    from .settings import AnimePushSettings

CredentialAlias = Literal["access_token", "anilist_token"]
CollectionTargetStatus = Literal["watching", "completed"]

T = TypeVar("T")

# 调用内租约必须随执行上下文隔离：`asyncio.to_thread` 会继承创建时的
# contextvars 副本，所以并发调用各自看到自己的租约，不会互相覆盖。
_CALL_LEASE: ContextVar[object | None] = ContextVar("bangumi_call_lease", default=None)


class CredentialProvider(Protocol):
    """宿主凭据短租约：只解析本插件固定输入里出现的引用。"""

    def open(
        self,
        ctx: object,
        refs: Mapping[str, CredentialRef],
    ) -> AbstractAsyncContextManager[object]: ...


class CredentialLease(Protocol):
    def credential(self, ref: CredentialRef) -> str: ...


class _LeaseHandle:
    """一次凭据租约：同时持有 client 与打开它的 context manager。

    宿主 ``CredentialClients.open`` 是 ``@asynccontextmanager``，退出时才
    ``aclose`` client。如果只取 ``__aenter__`` 而丢弃 manager，函数返回时
    生成器被回收会立刻执行 ``finally`` 里的 ``aclose``，client 一出生就是
    关闭状态。因此 manager 必须与 client 一起被持有。
    """

    __slots__ = ("_client", "_manager")

    def __init__(self, client: object, manager: object) -> None:
        self._client = client
        self._manager = manager

    def credential(self, ref: CredentialRef) -> str:
        reader = getattr(self._client, "credential", None)
        if not callable(reader):
            raise ToolExecutionError("凭据租约不可用")
        value = reader(ref)
        if not isinstance(value, str):
            raise ToolExecutionError("凭据内容无效")
        return value

    async def aclose(self) -> None:
        exit_ = getattr(self._manager, "__aexit__", None)
        if callable(exit_):
            await exit_(None, None, None)
        else:
            closer = getattr(self._client, "aclose", None)
            if callable(closer):
                await closer()


@dataclass
class BangumiRuntime:
    """一次 ``apply`` 拥有的全部运行状态。"""

    settings: BangumiSettings
    data_dir: Path
    credentials: CredentialProvider | None = None
    timers: PluginTimersPort | None = None
    ctx: object | None = None
    _client: BangumiClient | None = field(default=None, init=False, repr=False)
    _service: BangumiService | None = field(default=None, init=False, repr=False)
    _store: object | None = field(default=None, init=False, repr=False)
    _background_lease: object | None = field(default=None, init=False, repr=False)
    _loop: AnimeUpdateLoop | None = field(default=None, init=False, repr=False)
    _bridge: BangumiAlertBridge | None = field(default=None, init=False, repr=False)

    # ---- 发现合同 ----

    def catalog(self) -> object:
        """返回绑定到本运行时的工具目录。"""

        from .tools import build_tool_catalog

        return build_tool_catalog(invoke=self.invoke)

    # ---- 领域调用（同步实现，由 invoke 放到线程执行）----

    def _bangumi(self) -> BangumiService:
        if self._service is None:
            self._service = BangumiService(
                self._bangumi_client(),
                ConfirmationStore(),
                QueryConfirmationStore(),
                CollectionQuerySessionStore(),
            )
        return self._service

    def _bangumi_client(self) -> BangumiClient:
        """后台刷新直接使用 client：它提供 get_me / get_subject 等底层读取。

        ``BangumiService`` 只有面向工具的高层方法，没有这些接口；协调器此前
        被传入 service，导致 ``refresh_catalog`` 抛 AttributeError 后静默失败。
        """

        if self._client is None:
            from .client import BangumiClient

            self._client = BangumiClient(
                BangumiRuntimeConfig(
                    token_provider=lambda: self._token("access_token"),
                    user_agent=self.settings.user_agent,
                )
            )
        return self._client

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
                alert_sync=self.sync_pending_alerts,
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
            self._bangumi_client(),
            AniListClient(
                user_agent=self.settings.user_agent,
                token_provider=lambda: self._token("anilist_token"),
            ),
            self.settings.anime_push,
        )

    # ---- 告警投递桥（依赖第一方 eventmail）----

    async def deploy_alert_bridge(self, child: PluginContext) -> BangumiAlertBridge:
        """在 eventmail 可用的子 Fiber 上绑定告警与上下文来源。

        v2 的 ``bind`` 独占 ``source_id``，所以必须在同一 Fiber 上登记 ``close``
        Effect。``effect`` 本身也可能失败（Fiber 正在卸载），此时 binding 已拿到
        所有权却无人释放，会导致下次装载直接报「已有 owner」——因此逐个兜底。
        """

        alerts = child.require(ALERT_SOURCE).bind(SOURCE_ID)
        try:
            _ = await child.effect(
                lambda: alerts.close, label="bangumi-alert-source-binding",
            )
        except BaseException:
            alerts.close()
            raise
        contexts = child.require(CONTEXT_SOURCE).bind(SOURCE_ID)
        try:
            _ = await child.effect(
                lambda: contexts.close, label="bangumi-context-source-binding",
            )
        except BaseException:
            contexts.close()
            raise

        self._bridge = BangumiAlertBridge(
            store=self._anime_store(),
            binding=AlertBinding(alerts=alerts, contexts=contexts),
        )
        return self._bridge

    async def sync_pending_alerts(self) -> dict[str, int]:
        """把本地 pending 事件同步进告警邮箱；没有桥时是空操作。"""

        if self._bridge is None:
            return {"reported": 0, "acknowledged": 0}
        return await self._bridge.tick(datetime.now(timezone.utc))

    # ---- 工具入口 ----

    async def invoke(
        self,
        method: str,
        *args: object,
        **kwargs: object,
    ) -> ToolResultValue:
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

    async def _run_in_lease(
        self,
        work: Callable[[], T],
        *,
        allow_retry: bool = True,
    ) -> T:
        """在凭据租约内执行同步工作。

        调用自己打开的租约必须自己关闭；后台长期租约是共享资源，不能在这里
        ``aclose``，否则一次工具调用就会让后续所有调用（含后台刷新）永久失败。
        """

        lease, owned = await self._open_lease()
        if lease is None:
            return await asyncio.to_thread(work)
        token = _CALL_LEASE.set(lease)
        released = False
        try:
            return await asyncio.to_thread(work)
        except RuntimeError:
            # 业务异常（BangumiApiError / AniListApiError / AnimeMappingError 等）
            # 也都继承 RuntimeError，不能靠异常类型判断租约是否失效，否则一次网络
            # 抖动就会误清后台租约。这里只探测「租约还能不能读出凭据」。
            if not allow_retry or owned or not self._lease_is_dead(lease):
                raise
            # 重试发生在 finally 之前，必须先撤掉本层的上下文绑定，
            # 否则 _token() 仍会读到刚判定失效的租约。
            _CALL_LEASE.reset(token)
            released = True
            self._background_lease = None
            return await self._run_in_lease(work, allow_retry=False)
        finally:
            if not released:
                _CALL_LEASE.reset(token)
            if owned:
                closer = getattr(lease, "aclose", None)
                if callable(closer):
                    await closer()

    def _lease_is_dead(self, lease: object) -> bool:
        """租约是否已被宿主关闭：以能否读出本插件自己的凭据为准。"""

        reader = getattr(lease, "credential", None)
        if not callable(reader):
            return False
        try:
            _ = reader(self._credential_ref("access_token"))
        except RuntimeError:
            return True
        except Exception:  # noqa: BLE001 - 其它错误不代表租约失效
            return False
        return False

    async def _open_lease(self) -> tuple[object | None, bool]:
        """返回 ``(租约, 是否由本次调用独占)``。

        后台租约只借用、不关闭；临时租约由调用方负责关闭。
        """

        if self._background_lease is not None:
            return self._background_lease, False
        return await self._enter_lease(), True

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
        client = await manager.__aenter__()
        # manager 必须随 client 一起存活；单独丢弃它会让 client 立即被关闭。
        return _LeaseHandle(client, manager)

    def _token(self, alias: CredentialAlias) -> str:
        lease = _CALL_LEASE.get() or self._background_lease
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
