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
        self.status_writes: list[tuple[int, int]] = []
        self.episode_writes: list[tuple[int, list[int], int]] = []
        self.fail_status_write = False

    def get_subject(self, subject_id: int) -> dict[str, Any]:
        assert subject_id == 42
        return deepcopy(self.subject)

    def get_me(self) -> dict[str, Any]:
        return {"username": "tester"}

    def get_collection(
        self,
        username: str,
        subject_id: int,
    ) -> dict[str, Any] | None:
        assert username == "tester" and subject_id == 42
        return deepcopy(self.collection)

    def list_episode_collections(self, subject_id: int) -> list[dict[str, Any]]:
        assert subject_id == 42
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
