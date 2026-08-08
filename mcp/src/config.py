from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_USER_AGENT = (
    "akashic-plugins/bangumi-mcp/0.5.0 "
    "(https://github.com/akashic-plugins/bangumi-mcp)"
)


class BangumiConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnimePushRuntimeConfig:
    enabled: bool = False
    notify_before_minutes: int = 0
    display_timezone: str = "Asia/Shanghai"
    anilist_token: str | None = field(default=None, repr=False)
    media_id_overrides: Mapping[int, int] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class BangumiRuntimeConfig:
    access_token: str = field(repr=False)
    user_agent: str = DEFAULT_USER_AGENT
    anime_push: AnimePushRuntimeConfig = field(
        default_factory=AnimePushRuntimeConfig
    )


def load_runtime_config(data_dir: Path) -> BangumiRuntimeConfig:
    """读取插件私密配置，并避免把 Token 放入异常或对象 repr。"""

    path = data_dir / "config.local.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BangumiConfigError(
            f"缺少 Bangumi 私密配置: {path}"
        ) from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BangumiConfigError(f"无法读取 Bangumi 私密配置: {path}") from error

    token = raw.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise BangumiConfigError("config.local.toml 缺少非空 access_token")
    token = token.strip()
    if "\n" in token or "\r" in token:
        raise BangumiConfigError("access_token 不能包含换行")

    user_agent = raw.get("user_agent", DEFAULT_USER_AGENT)
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise BangumiConfigError("user_agent 必须是非空字符串")
    user_agent = user_agent.strip()
    if "\n" in user_agent or "\r" in user_agent:
        raise BangumiConfigError("user_agent 不能包含换行")

    anime_push = _anime_push_config(raw.get("anime_push", {}))
    return BangumiRuntimeConfig(
        access_token=token,
        user_agent=user_agent,
        anime_push=anime_push,
    )


def _anime_push_config(value: object) -> AnimePushRuntimeConfig:
    if not isinstance(value, dict):
        raise BangumiConfigError("anime_push 必须是 TOML table")

    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise BangumiConfigError("anime_push.enabled 必须是布尔值")

    notify_before = value.get("notify_before_minutes", 0)
    if (
        isinstance(notify_before, bool)
        or not isinstance(notify_before, int)
        or not 0 <= notify_before <= 1440
    ):
        raise BangumiConfigError(
            "anime_push.notify_before_minutes 必须是 0 至 1440 的整数"
        )

    display_timezone = value.get("display_timezone", "Asia/Shanghai")
    if not isinstance(display_timezone, str) or not display_timezone.strip():
        raise BangumiConfigError(
            "anime_push.display_timezone 必须是有效 IANA 时区"
        )
    display_timezone = display_timezone.strip()
    try:
        ZoneInfo(display_timezone)
    except ZoneInfoNotFoundError as error:
        raise BangumiConfigError(
            "anime_push.display_timezone 必须是有效 IANA 时区"
        ) from error

    raw_anilist_token = value.get("anilist_token")
    if raw_anilist_token is not None and not isinstance(raw_anilist_token, str):
        raise BangumiConfigError("anime_push.anilist_token 必须是字符串")
    anilist_token = (
        raw_anilist_token.strip() if isinstance(raw_anilist_token, str) else ""
    )
    if "\n" in anilist_token or "\r" in anilist_token:
        raise BangumiConfigError("anime_push.anilist_token 不能包含换行")

    raw_overrides = value.get("media_id_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise BangumiConfigError(
            "anime_push.media_id_overrides 必须是 TOML table"
        )
    overrides: dict[int, int] = {}
    for raw_subject_id, raw_media_id in raw_overrides.items():
        if isinstance(raw_subject_id, bool) or isinstance(raw_media_id, bool):
            raise BangumiConfigError("media_id_overrides 的 ID 必须是正整数")
        try:
            subject_id = int(raw_subject_id)
        except (TypeError, ValueError) as error:
            raise BangumiConfigError(
                "media_id_overrides 的 subject ID 必须是正整数"
            ) from error
        if (
            subject_id <= 0
            or not isinstance(raw_media_id, int)
            or raw_media_id <= 0
        ):
            raise BangumiConfigError("media_id_overrides 的 ID 必须是正整数")
        overrides[subject_id] = raw_media_id

    return AnimePushRuntimeConfig(
        enabled=enabled,
        notify_before_minutes=notify_before,
        display_timezone=display_timezone,
        anilist_token=anilist_token or None,
        media_id_overrides=MappingProxyType(overrides),
    )
