from __future__ import annotations

from datetime import datetime, timezone
from numbers import Real
from typing import Any, Literal

from .client import BangumiApiError, BangumiClient
from .confirmation import ConfirmationStore, PreparedUpdate


SUBJECT_ANIME = 2
EPISODE_WATCHED = 2
COLLECTION_TYPES = {
    1: "想看",
    2: "看过",
    3: "在看",
    4: "搁置",
    5: "抛弃",
}
SUBJECT_TYPES = {
    1: "书籍",
    2: "动画",
    3: "音乐",
    4: "游戏",
    6: "三次元",
}
STATUS_TARGETS = {
    "watching": (3, "在看"),
    "completed": (2, "看过"),
}


class BangumiInputError(ValueError):
    pass


class BangumiService:
    def __init__(
        self,
        client: BangumiClient,
        confirmations: ConfirmationStore,
    ) -> None:
        self._client = client
        self._confirmations = confirmations

    def get_collection_status(self, subject_id: int) -> dict[str, object]:
        """查询作品、收藏状态和逐集动画进度。"""

        subject_id = _positive_int(subject_id, "subject_id")
        subject = self._client.get_subject(subject_id)
        identity = _subject_identity(subject, subject_id)
        username = _username(self._client.get_me())
        collection = self._client.get_collection(username, subject_id)
        result: dict[str, object] = {
            "subject": identity,
            "collected": collection is not None,
            "collection_status": _collection_status(collection),
            "anime_progress": None,
        }
        if identity["type"] == SUBJECT_ANIME and collection is not None:
            episodes = self._client.list_episode_collections(subject_id)
            result["anime_progress"] = _anime_progress(episodes)
        return result

    def prepare_collection_status_update(
        self,
        subject_id: int,
        status: Literal["watching", "completed"],
    ) -> dict[str, object]:
        """只读预览条目状态更新，不执行 Bangumi 写入。"""

        subject_id = _positive_int(subject_id, "subject_id")
        target = STATUS_TARGETS.get(status)
        if target is None:
            raise BangumiInputError("status 只支持 watching 或 completed")
        target_type, target_label = target
        subject = self._client.get_subject(subject_id)
        identity = _subject_identity(subject, subject_id)
        username = _username(self._client.get_me())
        current = self._client.get_collection(username, subject_id)
        current_status = _collection_status(current)
        if current_status is not None and current_status["type"] == target_type:
            return {
                "requires_confirmation": False,
                "no_change": True,
                "subject": identity,
                "current_status": current_status,
                "target_status": {"type": target_type, "label": target_label},
            }

        title = str(identity["title"])
        confirmation_text = f"确认：将《{title}》设置为“{target_label}”"
        pending = self._confirmations.prepare(
            PreparedUpdate(
                kind="collection_status",
                subject_id=subject_id,
                subject_title=title,
                target_label=target_label,
                collection_type=target_type,
                confirmation_text=confirmation_text,
            )
        )
        return _confirmation_preview(
            pending.confirmation_id,
            pending.expires_at,
            confirmation_text,
            {
                "subject": identity,
                "current_status": current_status,
                "target_status": {"type": target_type, "label": target_label},
            },
        )

    def prepare_anime_progress_update(
        self,
        subject_id: int,
        episode_number: int,
    ) -> dict[str, object]:
        """只读预览动画进度更新，按章节 ID 解析第 1 至 N 集。"""

        subject_id = _positive_int(subject_id, "subject_id")
        episode_number = _positive_int(episode_number, "episode_number")
        subject = self._client.get_subject(subject_id)
        identity = _subject_identity(subject, subject_id)
        if identity["type"] != SUBJECT_ANIME:
            raise BangumiInputError("逐集进度只支持动画条目")
        username = _username(self._client.get_me())
        if self._client.get_collection(username, subject_id) is None:
            raise BangumiInputError("条目尚未收藏，请先明确设置为“在看”或“看过”")

        episodes = self._client.list_episode_collections(subject_id)
        target = _episode_by_number(episodes, episode_number)
        to_mark = _episodes_to_mark(episodes, episode_number)
        if not to_mark:
            return {
                "requires_confirmation": False,
                "no_change": True,
                "subject": identity,
                "target_episode": _episode_summary(target),
                "anime_progress": _anime_progress(episodes),
            }

        title = str(identity["title"])
        confirmation_text = (
            f"确认：将《{title}》动画进度更新到第 {episode_number} 集"
            "（逐集标记为看过）"
        )
        pending = self._confirmations.prepare(
            PreparedUpdate(
                kind="anime_progress",
                subject_id=subject_id,
                subject_title=title,
                target_label=f"第 {episode_number} 集",
                episode_number=episode_number,
                target_episode_id=_episode_id(target),
                confirmation_text=confirmation_text,
            )
        )
        return _confirmation_preview(
            pending.confirmation_id,
            pending.expires_at,
            confirmation_text,
            {
                "subject": identity,
                "target_episode": _episode_summary(target),
                "episodes_to_mark": [
                    _episode_number(item) for item in to_mark
                ],
                "episode_count": len(to_mark),
                "current_progress": _anime_progress(episodes),
            },
        )

    def commit_prepared_update(
        self,
        confirmation_id: str,
        confirmation_text: str,
    ) -> dict[str, object]:
        """消费一次性确认并执行唯一一次 Bangumi 写操作。"""

        operation = self._confirmations.consume(
            confirmation_id.strip(),
            confirmation_text,
        )
        subject = self._client.get_subject(operation.subject_id)
        identity = _subject_identity(subject, operation.subject_id)
        if identity["title"] != operation.subject_title:
            raise BangumiInputError("作品显示名称已经变化，请重新预览并确认")

        if operation.kind == "collection_status":
            if operation.collection_type is None:
                raise RuntimeError("收藏状态确认记录损坏")
            self._client.set_collection_type(
                operation.subject_id,
                operation.collection_type,
            )
            return {
                "updated": True,
                "subject": identity,
                "collection_status": {
                    "type": operation.collection_type,
                    "label": operation.target_label,
                },
            }

        if operation.episode_number is None or operation.target_episode_id is None:
            raise RuntimeError("动画进度确认记录损坏")
        username = _username(self._client.get_me())
        if self._client.get_collection(username, operation.subject_id) is None:
            raise BangumiInputError("条目已不在收藏中，请重新预览")
        episodes = self._client.list_episode_collections(operation.subject_id)
        target = _episode_by_number(episodes, operation.episode_number)
        if _episode_id(target) != operation.target_episode_id:
            raise BangumiInputError("目标章节已经变化，请重新预览并确认")
        to_mark = _episodes_to_mark(episodes, operation.episode_number)
        if not to_mark:
            return {
                "updated": False,
                "no_change": True,
                "subject": identity,
                "anime_progress": _anime_progress(episodes),
            }
        self._client.set_episode_collections(
            operation.subject_id,
            [_episode_id(item) for item in to_mark],
            collection_type=EPISODE_WATCHED,
        )
        return {
            "updated": True,
            "subject": identity,
            "target_episode": _episode_summary(target),
            "marked_episode_numbers": [
                _episode_number(item) for item in to_mark
            ],
        }


def _confirmation_preview(
    confirmation_id: str,
    expires_at: float,
    confirmation_text: str,
    target: dict[str, object],
) -> dict[str, object]:
    return {
        "requires_confirmation": True,
        "confirmation_id": confirmation_id,
        "expires_at": datetime.fromtimestamp(
            expires_at,
            tz=timezone.utc,
        ).isoformat(),
        "confirmation_text": confirmation_text,
        "target": target,
        "instruction": "显示 target 和 confirmation_text 后结束本轮，等待用户逐字确认。",
    }


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BangumiInputError(f"{label} 必须是正整数")
    return value


def _subject_identity(subject: dict[str, Any], subject_id: int) -> dict[str, object]:
    actual_id = subject.get("id")
    if actual_id != subject_id:
        raise BangumiApiError("Bangumi 返回的条目 ID 不匹配")
    subject_type = subject.get("type")
    if isinstance(subject_type, bool) or not isinstance(subject_type, int):
        raise BangumiApiError("Bangumi 返回的条目类型无效")
    name_cn = subject.get("name_cn")
    name = subject.get("name")
    raw_title = name_cn if isinstance(name_cn, str) and name_cn.strip() else name
    if not isinstance(raw_title, str) or not raw_title.strip():
        raise BangumiApiError("Bangumi 返回的作品名为空")
    title = " ".join(raw_title.split())[:160]
    return {
        "id": subject_id,
        "title": title,
        "type": subject_type,
        "type_label": SUBJECT_TYPES.get(subject_type, "未知"),
    }


def _username(payload: dict[str, Any]) -> str:
    username = payload.get("username")
    if not isinstance(username, str) or not username.strip():
        raise BangumiApiError("Bangumi 当前用户缺少 username")
    return username.strip()


def _collection_status(collection: dict[str, Any] | None) -> dict[str, object] | None:
    if collection is None:
        return None
    collection_type = collection.get("type")
    if isinstance(collection_type, bool) or not isinstance(collection_type, int):
        raise BangumiApiError("Bangumi 收藏状态无效")
    return {
        "type": collection_type,
        "label": COLLECTION_TYPES.get(collection_type, "未知"),
    }


def _episode_payload(item: dict[str, Any]) -> dict[str, Any]:
    episode = item.get("episode")
    if not isinstance(episode, dict):
        raise BangumiApiError("Bangumi 章节收藏缺少 episode")
    return episode


def _episode_id(item: dict[str, Any]) -> int:
    value = _episode_payload(item).get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BangumiApiError("Bangumi 章节 ID 无效")
    return value


def _episode_number(item: dict[str, Any]) -> int:
    value = _episode_payload(item).get("sort")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise BangumiApiError("Bangumi 章节序号无效")
    number = int(value)
    if number <= 0 or float(value) != number:
        raise BangumiApiError("Bangumi 本篇章节序号必须是正整数")
    return number


def _episode_collection_type(item: dict[str, Any]) -> int:
    value = item.get("type")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BangumiApiError("Bangumi 章节收藏状态无效")
    return value


def _episode_by_number(
    episodes: list[dict[str, Any]],
    episode_number: int,
) -> dict[str, Any]:
    matches = [item for item in episodes if _episode_number(item) == episode_number]
    if len(matches) != 1:
        raise BangumiInputError(
            f"无法唯一定位动画第 {episode_number} 集，找到 {len(matches)} 条"
        )
    return matches[0]


def _episodes_to_mark(
    episodes: list[dict[str, Any]],
    episode_number: int,
) -> list[dict[str, Any]]:
    selected = [
        item
        for item in episodes
        if _episode_number(item) <= episode_number
        and _episode_collection_type(item) != EPISODE_WATCHED
    ]
    return sorted(selected, key=_episode_number)


def _episode_summary(item: dict[str, Any]) -> dict[str, object]:
    episode = _episode_payload(item)
    name_cn = episode.get("name_cn")
    name = episode.get("name")
    raw_title = name_cn if isinstance(name_cn, str) and name_cn.strip() else name
    title = " ".join(raw_title.split())[:160] if isinstance(raw_title, str) else ""
    return {
        "id": _episode_id(item),
        "number": _episode_number(item),
        "title": title,
        "collection_type": _episode_collection_type(item),
    }


def _anime_progress(episodes: list[dict[str, Any]]) -> dict[str, object]:
    by_number: dict[int, int] = {}
    for item in episodes:
        number = _episode_number(item)
        if number in by_number:
            raise BangumiApiError(f"Bangumi 本篇章节序号重复: {number}")
        by_number[number] = _episode_collection_type(item)
    watched = sorted(
        number
        for number, collection_type in by_number.items()
        if collection_type == EPISODE_WATCHED
    )
    watched_through = 0
    while by_number.get(watched_through + 1) == EPISODE_WATCHED:
        watched_through += 1
    highest = watched[-1] if watched else 0
    gaps = [
        number
        for number in range(1, highest)
        if by_number.get(number) != EPISODE_WATCHED
    ]
    return {
        "highest_watched_episode": highest,
        "watched_through_episode": watched_through,
        "watched_episode_count": len(watched),
        "unwatched_before_highest": gaps,
        "main_episode_count": len(by_number),
    }
