from __future__ import annotations

import pytest

from src.confirmation import (
    ConfirmationError,
    ConfirmationStore,
    PreparedUpdate,
)


def operation() -> PreparedUpdate:
    return PreparedUpdate(
        kind="collection_status",
        subject_id=42,
        subject_title="测试动画",
        target_label="在看",
        confirmation_text="确认：将《测试动画》设置为“在看”",
        collection_type=3,
    )


def test_confirmation_is_exact_and_single_use() -> None:
    store = ConfirmationStore()
    pending = store.prepare(operation())

    with pytest.raises(ConfirmationError, match="不匹配"):
        store.consume(pending.confirmation_id, "确认")

    assert store.consume(
        pending.confirmation_id,
        "  确认：将《测试动画》设置为“在看”  ",
    ) == operation()
    with pytest.raises(ConfirmationError, match="不存在或已经使用"):
        store.consume(pending.confirmation_id, operation().confirmation_text)


def test_expired_confirmation_cannot_be_used() -> None:
    now = [100.0]
    store = ConfirmationStore(ttl_seconds=10, clock=lambda: now[0])
    pending = store.prepare(operation())
    now[0] = 110.0

    with pytest.raises(ConfirmationError, match="已过期"):
        store.consume(pending.confirmation_id, operation().confirmation_text)
