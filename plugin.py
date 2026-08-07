from __future__ import annotations

from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from agent.plugins import McpServerSpec, Plugin, ProactiveSourceSpec


DEFAULT_USER_AGENT = (
    "akashic-plugins/bangumi-mcp/0.5.0 "
    "(https://github.com/akashic-plugins/bangumi-mcp)"
)


class AnimePushConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, strict=True)
    notify_before_minutes: int = Field(default=0, ge=0, le=1440, strict=True)
    display_timezone: str = "Asia/Shanghai"
    anilist_token: SecretStr | None = None
    media_id_overrides: dict[int, int] = Field(default_factory=dict)

    @field_validator("display_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("display_timezone 必须是非空时区名称")
        try:
            ZoneInfo(clean)
        except ZoneInfoNotFoundError as error:
            raise ValueError("display_timezone 不是有效 IANA 时区") from error
        return clean

    @field_validator("media_id_overrides", mode="before")
    @classmethod
    def validate_overrides(cls, value: object) -> dict[int, int]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("media_id_overrides 必须是映射")
        result: dict[int, int] = {}
        for raw_subject_id, raw_media_id in value.items():
            if isinstance(raw_subject_id, bool) or isinstance(raw_media_id, bool):
                raise ValueError("media_id_overrides 的 ID 必须是正整数")
            try:
                subject_id = int(raw_subject_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "media_id_overrides 的 subject ID 必须是正整数"
                ) from error
            if (
                subject_id <= 0
                or not isinstance(raw_media_id, int)
                or raw_media_id <= 0
            ):
                raise ValueError("media_id_overrides 的 ID 必须是正整数")
            result[subject_id] = raw_media_id
        return result


class BangumiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: SecretStr | None = None
    user_agent: str = Field(default=DEFAULT_USER_AGENT, min_length=1)
    anime_push: AnimePushConfig = Field(default_factory=AnimePushConfig)


class BangumiPlugin(Plugin):
    api_version = 2
    name = "bangumi"
    version = "0.5.0"
    desc = "查询 Bangumi 收藏与观看进度，并主动提醒在看动画的计划放送"
    author = "lfegg"
    ConfigModel = BangumiConfig

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [
            McpServerSpec(
                name="bangumi",
                command=("python", "mcp/run_mcp.py"),
            )
        ]

    def proactive_sources(self) -> list[ProactiveSourceSpec]:
        config = self.context.config
        if not isinstance(config, BangumiConfig) or not config.anime_push.enabled:
            return []
        return [
            ProactiveSourceSpec(
                id="anime_updates",
                channels=("alert",),
                server="bangumi",
                fetch_tool="get_anime_update_alerts",
                ack_tool="acknowledge_anime_update_alerts",
                fetch_page_size=50,
            )
        ]
