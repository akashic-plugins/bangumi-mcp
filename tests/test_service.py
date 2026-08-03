from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.client import BangumiApiError
from src.confirmation import ConfirmationError, ConfirmationStore
from src.service import BangumiInputError, BangumiService


def episode(number: int, status: int, *, episode_id: int | None = None) -> dict[str, Any]:
    return {
        "episode": {
            "id": episode_id or 1000 + number,
            "sort": number,
            "name": f"Episode {number}",
            "name_cn": f"第 {number} 集",
        },
        "type": status,
    }


def collection_item(
    subject_id: int,
    *,
    subject_type: int = 2,
    collection_type: int = 3,
    include_subject: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "subject_id": subject_id,
        "subject_type": subject_type,
        "rate": 8,
        "type": collection_type,
        "comment": "不应进入结果",
        "tags": ["不应进入结果"],
        "ep_status": 5,
        "vol_status": 0,
        "updated_at": "2026-08-03T12:00:00+08:00",
        "private": False,
    }
    if include_subject:
        item["subject"] = {
            "id": subject_id,
            "type": subject_type,
            "name": f"Subject {subject_id}",
            "name_cn": f"作品 {subject_id}",
            "eps": 12,
            "volumes": 0,
        }
    return item


class FakeClient:
    def __init__(
        self,
        *,
        subject_type: int = 2,
        collection_type: int | None = 3,
        episodes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.subject = {
            "id": 42,
            "type": subject_type,
            "name": "Test Anime",
            "name_cn": "测试动画",
        }
        self.collection = (
            None if collection_type is None else {"type": collection_type}
        )
        self.episodes = episodes or [episode(1, 0), episode(2, 0), episode(3, 0)]
        self.collection_page: dict[str, Any] = {
            "total": 1,
            "limit": 10,
            "offset": 0,
            "data": [collection_item(42)],
        }
        self.get_me_calls = 0
        self.subject_reads = 0
        self.episode_reads = 0
        self.list_collection_calls: list[dict[str, Any]] = []
        self.status_writes: list[tuple[int, int]] = []
        self.episode_writes: list[tuple[int, list[int], int]] = []
        self.fail_status_write = False

    def get_subject(self, subject_id: int) -> dict[str, Any]:
        assert subject_id == 42
        self.subject_reads += 1
        return deepcopy(self.subject)

    def get_me(self) -> dict[str, Any]:
        self.get_me_calls += 1
        return {"username": "tester"}

    def list_collections(
        self,
        username: str,
        *,
        subject_type: int | None,
        collection_type: int | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        assert username == "tester"
        self.list_collection_calls.append(
            {
                "subject_type": subject_type,
                "collection_type": collection_type,
                "limit": limit,
                "offset": offset,
            }
        )
        return deepcopy(self.collection_page)

    def get_collection(
        self,
        username: str,
        subject_id: int,
    ) -> dict[str, Any] | None:
        assert username == "tester" and subject_id == 42
        return deepcopy(self.collection)

    def list_episode_collections(self, subject_id: int) -> list[dict[str, Any]]:
        assert subject_id == 42
        self.episode_reads += 1
        return deepcopy(self.episodes)

    def set_collection_type(self, subject_id: int, collection_type: int) -> None:
        if self.fail_status_write:
            raise BangumiApiError("remote outcome unknown")
        self.status_writes.append((subject_id, collection_type))

    def set_episode_collections(
        self,
        subject_id: int,
        episode_ids: list[int],
        *,
        collection_type: int,
    ) -> None:
        self.episode_writes.append((subject_id, episode_ids, collection_type))


def service(client: FakeClient) -> BangumiService:
    return BangumiService(client, ConfirmationStore())  # type: ignore[arg-type]


def test_collection_list_defaults_to_ten_and_returns_bounded_summary() -> None:
    client = FakeClient()

    result = service(client).list_collections()

    assert result["filters"] == {"subject_type": "all", "status": "all"}
    assert result["page"] == {
        "total": 1,
        "limit": 10,
        "offset": 0,
        "returned": 1,
        "has_more": False,
        "next_offset": None,
    }
    assert result["collections"] == [
        {
            "subject": {
                "id": 42,
                "title": "作品 42",
                "type": 2,
                "type_value": "anime",
                "type_label": "动画",
                "episodes": 12,
                "volumes": 0,
            },
            "collection": {
                "type": 3,
                "status": "watching",
                "status_label": "在看",
                "rating": 8,
                "reported_episode_progress": 5,
                "reported_volume_progress": 0,
                "private": False,
                "updated_at": "2026-08-03T12:00:00+08:00",
            },
        }
    ]
    assert client.list_collection_calls == [
        {
            "subject_type": None,
            "collection_type": None,
            "limit": 10,
            "offset": 0,
        }
    ]
    assert client.get_me_calls == 1
    assert client.subject_reads == 0
    assert client.episode_reads == 0


def test_collection_list_maps_filters_and_preserves_missing_subject() -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 12,
        "limit": 1,
        "offset": 10,
        "data": [collection_item(99, include_subject=False)],
    }

    result = service(client).list_collections(
        subject_type="anime",
        status="watching",
        limit=1,
        offset=10,
    )

    assert client.list_collection_calls[0] == {
        "subject_type": 2,
        "collection_type": 3,
        "limit": 1,
        "offset": 10,
    }
    assert result["page"]["has_more"] is True
    assert result["page"]["next_offset"] == 11
    assert result["collections"][0]["subject"] == {
        "id": 99,
        "title": None,
        "type": 2,
        "type_value": "anime",
        "type_label": "动画",
        "episodes": None,
        "volumes": None,
    }
    assert client.subject_reads == 0
    assert client.episode_reads == 0


@pytest.mark.parametrize(
    ("subject_type", "api_value"),
    [("book", 1), ("anime", 2), ("music", 3), ("game", 4), ("real", 6)],
)
def test_collection_list_maps_each_subject_type(
    subject_type: str,
    api_value: int,
) -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 0,
        "limit": 10,
        "offset": 0,
        "data": [],
    }

    service(client).list_collections(  # type: ignore[arg-type]
        subject_type=subject_type
    )

    assert client.list_collection_calls[0]["subject_type"] == api_value


@pytest.mark.parametrize(
    ("status", "api_value"),
    [
        ("wish", 1),
        ("completed", 2),
        ("watching", 3),
        ("on_hold", 4),
        ("dropped", 5),
    ],
)
def test_collection_list_maps_each_status(status: str, api_value: int) -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 0,
        "limit": 10,
        "offset": 0,
        "data": [],
    }

    service(client).list_collections(status=status)  # type: ignore[arg-type]

    assert client.list_collection_calls[0]["collection_type"] == api_value


@pytest.mark.parametrize(
    "kwargs",
    [
        {"subject_type": "invalid"},
        {"status": "invalid"},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"offset": -1},
        {"offset": False},
    ],
)
def test_collection_list_rejects_invalid_inputs(kwargs: dict[str, Any]) -> None:
    client = FakeClient()

    with pytest.raises(BangumiInputError):
        service(client).list_collections(**kwargs)  # type: ignore[arg-type]
    assert client.get_me_calls == 0
    assert client.list_collection_calls == []


def test_collection_list_rejects_early_empty_page() -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 3,
        "limit": 10,
        "offset": 0,
        "data": [],
    }

    with pytest.raises(BangumiApiError, match="提前结束"):
        service(client).list_collections()


def test_collection_list_accepts_empty_page_beyond_total() -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 3,
        "limit": 10,
        "offset": 10,
        "data": [],
    }

    result = service(client).list_collections(offset=10)

    assert result["page"] == {
        "total": 3,
        "limit": 10,
        "offset": 10,
        "returned": 0,
        "has_more": False,
        "next_offset": None,
    }


def test_query_distinguishes_highest_and_contiguous_progress() -> None:
    client = FakeClient(
        episodes=[episode(1, 2), episode(2, 0), episode(3, 2)]
    )

    result = service(client).get_collection_status(42)

    assert result["collection_status"] == {"type": 3, "label": "在看"}
    assert result["anime_progress"] == {
        "highest_watched_episode": 3,
        "watched_through_episode": 1,
        "watched_episode_count": 2,
        "unwatched_before_highest": [2],
        "main_episode_count": 3,
    }


def test_status_update_requires_preview_and_exact_confirmation() -> None:
    client = FakeClient()
    target = service(client)

    preview = target.prepare_collection_status_update(42, "completed")

    assert client.status_writes == []
    assert preview["target"]["subject"]["title"] == "测试动画"
    with pytest.raises(ConfirmationError):
        target.commit_prepared_update(str(preview["confirmation_id"]), "确认")
    assert client.status_writes == []

    result = target.commit_prepared_update(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )
    assert client.status_writes == [(42, 2)]
    assert result["collection_status"] == {"type": 2, "label": "看过"}


def test_progress_update_marks_explicit_main_episode_ids_through_target() -> None:
    client = FakeClient(
        episodes=[episode(1, 2), episode(2, 0), episode(3, 0), episode(4, 0)]
    )
    target = service(client)

    preview = target.prepare_anime_progress_update(42, 3)

    assert client.episode_writes == []
    assert preview["target"]["episode_count"] == 2
    assert preview["target"]["episodes_to_mark"] == [2, 3]
    target.commit_prepared_update(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )
    assert client.episode_writes == [(42, [1002, 1003], 2)]


def test_progress_update_rejects_non_anime_and_uncollected_subject() -> None:
    with pytest.raises(BangumiInputError, match="只支持动画"):
        service(FakeClient(subject_type=1)).prepare_anime_progress_update(42, 1)
    with pytest.raises(BangumiInputError, match="尚未收藏"):
        service(FakeClient(collection_type=None)).prepare_anime_progress_update(42, 1)


def test_unknown_remote_result_consumes_confirmation_before_write() -> None:
    client = FakeClient()
    client.fail_status_write = True
    target = service(client)
    preview = target.prepare_collection_status_update(42, "completed")

    with pytest.raises(BangumiApiError, match="unknown"):
        target.commit_prepared_update(
            str(preview["confirmation_id"]),
            str(preview["confirmation_text"]),
        )
    with pytest.raises(ConfirmationError, match="不存在或已经使用"):
        target.commit_prepared_update(
            str(preview["confirmation_id"]),
            str(preview["confirmation_text"]),
        )
