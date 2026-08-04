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
    rating: int = 8,
    include_subject: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "subject_id": subject_id,
        "subject_type": subject_type,
        "rate": rating,
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
        self.collection_pages: list[dict[str, Any]] = []
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
        if self.collection_pages:
            return deepcopy(self.collection_pages.pop(0))
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
        "displayed": 1,
        "returned": 1,
        "has_more": False,
        "query_id": None,
        "query_expires_at": None,
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


def test_ordinary_list_returns_only_first_10_of_24_items() -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 24,
        "limit": 10,
        "offset": 0,
        "data": [
            collection_item(subject_id, collection_type=4)
            for subject_id in range(1, 11)
        ],
    }

    result = service(client).list_collections(
        subject_type="anime",
        status="on_hold",
    )

    assert result["page"]["total"] == 24
    assert result["page"]["limit"] == 10
    assert result["page"]["displayed"] == 10
    assert result["page"]["returned"] == 10
    assert result["page"]["has_more"] is True
    assert isinstance(result["page"]["query_id"], str)
    assert "offset" not in result["page"]
    assert len(result["collections"]) == 10
    assert client.list_collection_calls[0]["limit"] == 10


def test_collection_query_continues_with_opaque_id_and_preserves_missing_subject() -> None:
    client = FakeClient()
    client.collection_pages = [
        {
            "total": 11,
            "limit": 10,
            "offset": 0,
            "data": [collection_item(i) for i in range(1, 11)],
        },
        {
            "total": 11,
            "limit": 10,
            "offset": 10,
            "data": [collection_item(99, include_subject=False)],
        },
    ]
    target = service(client)
    first = target.list_collections(
        subject_type="anime",
        status="watching",
    )
    result = target.continue_collection_query(str(first["page"]["query_id"]))

    assert client.list_collection_calls[1] == {
        "subject_type": 2,
        "collection_type": 3,
        "limit": 10,
        "offset": 10,
    }
    assert result["page"]["has_more"] is False
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


def test_ordinary_query_requires_confirmation_before_reading_items_91_to_100() -> None:
    client = FakeClient()
    client.collection_pages = [
        *[
            {
                "total": 100,
                "limit": 10,
                "offset": offset,
                "data": [
                    collection_item(subject_id)
                    for subject_id in range(offset + 1, offset + 11)
                ],
            }
            for offset in range(0, 90, 10)
        ],
        {
            "total": 100,
            "limit": 50,
            "offset": 90,
            "data": [collection_item(subject_id) for subject_id in range(91, 101)],
        },
    ]
    target = service(client)
    result = target.list_collections()
    query_id = str(result["page"]["query_id"])
    for _ in range(8):
        result = target.continue_collection_query(query_id)

    assert result["page"]["displayed"] == 90
    assert len(client.list_collection_calls) == 9
    preview = target.continue_collection_query(query_id)
    assert preview["requires_confirmation"] is True
    assert preview["target"]["already_read"] == 90
    assert preview["target"]["trigger"]["at_least_100_items"] is True
    assert len(client.list_collection_calls) == 9

    final_page = target.execute_prepared_collection_query(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )
    assert len(client.list_collection_calls) == 10
    assert client.list_collection_calls[-1] == {
        "subject_type": None,
        "collection_type": None,
        "limit": 50,
        "offset": 90,
    }
    assert final_page["page"]["displayed"] == 100
    assert final_page["page"]["has_more"] is False


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


def test_collection_count_reads_one_item_only() -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 24,
        "limit": 1,
        "offset": 0,
        "data": [collection_item(1, collection_type=4)],
    }

    result = service(client).count_collections(
        subject_type="anime",
        status="on_hold",
    )

    assert result == {
        "user": {"username": "tester"},
        "filters": {"subject_type": "anime", "status": "on_hold"},
        "total": 24,
    }
    assert client.list_collection_calls == [
        {
            "subject_type": 2,
            "collection_type": 4,
            "limit": 1,
            "offset": 0,
        }
    ]


def test_collection_count_of_1000_does_not_require_confirmation() -> None:
    client = FakeClient()
    client.collection_page = {
        "total": 1000,
        "limit": 1,
        "offset": 0,
        "data": [collection_item(1)],
    }

    result = service(client).count_collections()

    assert result["total"] == 1000
    assert "requires_confirmation" not in result
    assert [call["limit"] for call in client.list_collection_calls] == [1]


def test_complete_24_item_query_requires_prepare_then_confirmation() -> None:
    client = FakeClient()
    client.collection_pages = [
        {
            "total": 24,
            "limit": 1,
            "offset": 0,
            "data": [collection_item(1, collection_type=4)],
        },
        {
            "total": 24,
            "limit": 50,
            "offset": 0,
            "data": [
                collection_item(subject_id, collection_type=4)
                for subject_id in range(1, 25)
            ],
        },
    ]
    target = service(client)
    preview = target.prepare_collection_query(
        subject_type="anime",
        status="on_hold",
    )

    assert preview["requires_confirmation"] is True
    assert preview["target"]["candidate_total"] == 24
    assert preview["target"]["trigger"] == {
        "complete_query": True,
        "at_least_100_items": False,
    }
    assert len(client.list_collection_calls) == 1
    result = target.execute_prepared_collection_query(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )
    assert result["complete"] is True
    assert result["candidate_total"] == 24
    assert result["returned"] == 24
    assert result["api_page_size"] == 50
    assert result["request_count"] == 1
    assert len(result["collections"]) == 24
    assert [call["limit"] for call in client.list_collection_calls] == [1, 50]


def test_rating_filter_scans_250_items_in_five_bulk_requests_without_cap() -> None:
    client = FakeClient()
    client.collection_pages = [
        {
            "total": 250,
            "limit": 1,
            "offset": 0,
            "data": [collection_item(1)],
        },
        *[
            {
                "total": 250,
                "limit": 50,
                "offset": offset,
                "data": [
                    collection_item(
                        subject_id,
                        rating=9 if subject_id % 2 else 7,
                    )
                    for subject_id in range(offset + 1, offset + 51)
                ],
            }
            for offset in range(0, 250, 50)
        ],
    ]
    target = service(client)
    preview = target.prepare_collection_query(operation="filter", min_rating=8)

    assert preview["target"]["candidate_total"] == 250
    assert preview["target"]["estimated_list_requests"] == 5
    result = target.execute_prepared_collection_query(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )

    assert result["matched_total"] == 125
    assert result["returned"] == 10
    assert result["has_more"] is True
    assert result["request_count"] == 5
    assert [call["limit"] for call in client.list_collection_calls] == [1] + [50] * 5
    assert [call["offset"] for call in client.list_collection_calls[1:]] == [
        0,
        50,
        100,
        150,
        200,
    ]


def test_complete_query_rejects_total_drift_and_duplicate_subjects() -> None:
    drift_client = FakeClient()
    drift_client.collection_pages = [
        {
            "total": 75,
            "limit": 1,
            "offset": 0,
            "data": [collection_item(1)],
        },
        {
            "total": 75,
            "limit": 50,
            "offset": 0,
            "data": [collection_item(i) for i in range(1, 51)],
        },
        {
            "total": 74,
            "limit": 50,
            "offset": 50,
            "data": [collection_item(i) for i in range(51, 75)],
        },
    ]
    drift_target = service(drift_client)
    drift_preview = drift_target.prepare_collection_query()
    with pytest.raises(BangumiApiError, match="不一致"):
        drift_target.execute_prepared_collection_query(
            str(drift_preview["confirmation_id"]),
            str(drift_preview["confirmation_text"]),
        )

    duplicate_client = FakeClient()
    duplicate_client.collection_pages = [
        {
            "total": 51,
            "limit": 1,
            "offset": 0,
            "data": [collection_item(1)],
        },
        {
            "total": 51,
            "limit": 50,
            "offset": 0,
            "data": [collection_item(i) for i in range(1, 51)],
        },
        {
            "total": 51,
            "limit": 50,
            "offset": 50,
            "data": [collection_item(50)],
        },
    ]
    duplicate_target = service(duplicate_client)
    duplicate_preview = duplicate_target.prepare_collection_query()
    with pytest.raises(BangumiApiError, match="重复"):
        duplicate_target.execute_prepared_collection_query(
            str(duplicate_preview["confirmation_id"]),
            str(duplicate_preview["confirmation_text"]),
        )


def test_analyze_query_returns_aggregate_without_raw_collections() -> None:
    client = FakeClient()
    client.collection_pages = [
        {
            "total": 2,
            "limit": 1,
            "offset": 0,
            "data": [collection_item(1, rating=8)],
        },
        {
            "total": 2,
            "limit": 50,
            "offset": 0,
            "data": [collection_item(1, rating=8), collection_item(2, rating=0)],
        },
    ]
    target = service(client)
    preview = target.prepare_collection_query(operation="analyze")
    result = target.execute_prepared_collection_query(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )

    assert result["returned"] == 0
    assert result["collections"] == []
    assert result["analysis"]["total"] == 2
    assert result["analysis"]["rated_count"] == 1
    assert result["analysis"]["average_rating"] == 8.0


def test_query_confirmation_is_exact_single_use_and_separate_from_write() -> None:
    client = FakeClient()
    client.collection_pages = [
        {"total": 0, "limit": 1, "offset": 0, "data": []},
        {"total": 0, "limit": 50, "offset": 0, "data": []},
    ]
    target = service(client)
    preview = target.prepare_collection_query()

    with pytest.raises(RuntimeError, match="不匹配"):
        target.execute_prepared_collection_query(
            str(preview["confirmation_id"]),
            "确认",
        )
    with pytest.raises(ConfirmationError, match="不存在或已经使用"):
        target.commit_prepared_update(
            str(preview["confirmation_id"]),
            str(preview["confirmation_text"]),
        )
    result = target.execute_prepared_collection_query(
        str(preview["confirmation_id"]),
        str(preview["confirmation_text"]),
    )
    assert result["candidate_total"] == 0
    with pytest.raises(RuntimeError, match="不存在或已经使用"):
        target.execute_prepared_collection_query(
            str(preview["confirmation_id"]),
            str(preview["confirmation_text"]),
        )


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
