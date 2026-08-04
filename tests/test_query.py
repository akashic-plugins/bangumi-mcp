from __future__ import annotations

import pytest

from src.query import (
    CollectionQuerySessionStore,
    PreparedCollectionQuery,
    QueryConfirmationStore,
    QueryStateError,
)


def item(subject_id: int) -> dict[str, object]:
    return {"subject": {"id": subject_id}}


def plan() -> PreparedCollectionQuery:
    return PreparedCollectionQuery(
        operation="list_all",
        username="tester",
        subject_type="anime",
        status="watching",
        candidate_total=100,
        confirmation_text="确认：完整读取 100 条收藏",
    )


def test_query_confirmation_is_exact_expires_and_is_single_use() -> None:
    now = [100.0]
    store = QueryConfirmationStore(ttl_seconds=10, clock=lambda: now[0])
    first = store.prepare(plan())

    with pytest.raises(QueryStateError, match="不匹配"):
        store.consume(first.confirmation_id, "确认")
    assert store.consume(first.confirmation_id, plan().confirmation_text) == plan()
    with pytest.raises(QueryStateError, match="不存在或已经使用"):
        store.consume(first.confirmation_id, plan().confirmation_text)

    expired = store.prepare(plan())
    now[0] = 110.0
    with pytest.raises(QueryStateError, match="已过期"):
        store.consume(expired.confirmation_id, plan().confirmation_text)


def test_query_id_is_bound_to_user_and_expires_before_fetch() -> None:
    now = [100.0]
    store = CollectionQuerySessionStore(ttl_seconds=10, clock=lambda: now[0])
    query_id, _ = store.create_page_session(
        username="tester",
        subject_type="all",
        status="all",
        total=20,
        items=[item(i) for i in range(1, 11)],
    )
    assert query_id is not None

    with pytest.raises(QueryStateError, match="不存在或已过期"):
        store.next_action("made-up", "tester")
    with pytest.raises(QueryStateError, match="不存在或已过期"):
        store.next_action(query_id, "another-user")
    now[0] = 110.0
    with pytest.raises(QueryStateError, match="不存在或已过期"):
        store.next_action(query_id, "tester")


def test_cancel_fetch_releases_busy_session() -> None:
    store = CollectionQuerySessionStore()
    query_id, _ = store.create_page_session(
        username="tester",
        subject_type="all",
        status="all",
        total=20,
        items=[item(i) for i in range(1, 11)],
    )
    assert query_id is not None
    action = store.next_action(query_id, "tester")
    assert action.kind == "fetch"
    with pytest.raises(QueryStateError, match="正在读取"):
        store.next_action(query_id, "tester")

    store.cancel_fetch(action)
    retry = store.next_action(query_id, "tester")
    assert retry.kind == "fetch"
    assert retry.offset == 10
