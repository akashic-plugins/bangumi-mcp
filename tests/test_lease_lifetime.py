"""凭据租约生命周期：后台长期租约不能被工具调用关闭。

真机事故：一次工具调用结束后，``_run_in_lease`` 的 ``finally`` 对借来的
后台租约也调用了 ``aclose()``，导致此后所有调用（包括后台刷新）都报
``provider credential client 已关闭``，且不重启无法恢复。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bangumi.runtime import build_runtime
from bangumi.settings import parse_settings
from tests.test_apply_registration import (
    _CONFIG,
    _CredentialServiceRecorder,
    _FakeContext,
)


def _runtime_with_background_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, _CredentialServiceRecorder]:
    from bangumi.client import BangumiClient
    from tests.test_apply_registration import _stub_api

    credentials = _CredentialServiceRecorder({"bangumi-token": "secret-token"})
    ctx = _FakeContext(config=_CONFIG, data_root=tmp_path, services={})
    monkeypatch.setattr(
        BangumiClient,
        "_request_json",
        lambda self, method, path, **kwargs: _stub_api(path),
    )
    runtime = build_runtime(
        settings=parse_settings(_CONFIG),
        data_dir=tmp_path,
        credentials=credentials,
        timers=None,
        ctx=ctx,
    )
    return runtime, credentials


def _new_lease(credentials: _CredentialServiceRecorder) -> Any:
    """造一个尚未关闭的租约（与 CredentialServiceRecorder 内部同类）。"""

    from tests.test_apply_registration import _Lease

    return _Lease({"bangumi-token": "secret-token"})


def test_tool_call_does_not_close_the_background_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """借用后台租约执行工具调用后，该租约必须仍然可用。"""

    runtime, credentials = _runtime_with_background_lease(tmp_path, monkeypatch)
    lease = _new_lease(credentials)
    runtime._background_lease = lease

    assert asyncio.run(runtime._run_in_lease(lambda: "ok")) == "ok"

    assert lease.closed is False, "工具调用关闭了共享的后台租约"
    assert runtime._background_lease is lease


def test_second_call_succeeds_after_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续两次调用都要成功——事故里第二次必失败。"""

    runtime, credentials = _runtime_with_background_lease(tmp_path, monkeypatch)
    lease = _new_lease(credentials)
    runtime._background_lease = lease

    first = asyncio.run(runtime.invoke("list_collections", subject_type="anime"))
    second = asyncio.run(runtime.invoke("list_collections", subject_type="anime"))

    assert first.outcome == "success"
    assert second.outcome == "success", second.parts[0].value
    assert lease.closed is False


def test_closed_background_lease_is_dropped_and_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主换代关闭了租约时，丢弃缓存并重建，而不是永久失败。"""

    runtime, credentials = _runtime_with_background_lease(tmp_path, monkeypatch)
    stale = _new_lease(credentials)
    runtime._background_lease = stale
    stale.closed = True  # 模拟宿主 remove_factory -> aclose()

    def read_token() -> str:
        return runtime._token("access_token")

    value = asyncio.run(runtime._run_in_lease(read_token))

    assert value == "secret-token"
    # 失效的租约必须从缓存里移除，后续调用改走临时租约。
    assert runtime._background_lease is not stale, "失效的租约必须被丢弃"


def test_own_lease_is_still_closed_after_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有后台租约时，调用自己开的租约仍要关闭，不能变成泄漏。"""

    runtime, credentials = _runtime_with_background_lease(tmp_path, monkeypatch)

    assert asyncio.run(runtime._run_in_lease(lambda: "ok")) == "ok"

    owned = credentials.leases[-1]
    assert owned.closed is True, "自开的租约必须关闭"


def test_concurrent_calls_keep_their_own_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发调用各自持有租约；不能因为共享实例字段互相覆盖。

    事故形态：``_lease`` 曾是实例字段，一个调用结束时把它清空，
    另一个正在执行的调用读 `_token()` 就报「凭据租约不可用」。
    """

    # 并发场景要求每次 open 都拿到独立的租约对象，因此这里单独构造 service，
    # 不复用共享同一对象的 _CredentialServiceRecorder。
    from bangumi.client import BangumiClient
    from tests.test_apply_registration import _Lease, _stub_api

    monkeypatch.setattr(
        BangumiClient,
        "_request_json",
        lambda self, method, path, **kwargs: _stub_api(path),
    )

    class _PerCallCreds:
        def open(self, ctx: object, refs: object) -> object:
            from contextlib import asynccontextmanager

            lease = _Lease({"bangumi-token": "secret-token"})

            @asynccontextmanager
            async def manager() -> object:
                yield lease

            return manager()

    runtime = build_runtime(
        settings=parse_settings(_CONFIG),
        data_dir=tmp_path,
        credentials=_PerCallCreds(),  # type: ignore[arg-type]
        timers=None,
        ctx=_FakeContext(config=_CONFIG, data_root=tmp_path, services={}),
    )

    def read_token() -> str:
        return runtime._token("access_token")

    async def exercise() -> list[object]:
        return await asyncio.gather(
            *[runtime._run_in_lease(read_token) for _ in range(8)],
            return_exceptions=True,
        )

    results = asyncio.run(exercise())

    failures = [item for item in results if isinstance(item, BaseException)]
    assert failures == [], f"并发调用互相踩租约: {failures[:2]}"
    assert all(value == "secret-token" for value in results)
