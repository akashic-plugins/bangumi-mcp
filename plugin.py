from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr

from agent.plugins import McpServerSpec, Plugin


DEFAULT_USER_AGENT = (
    "akashic-plugins/bangumi-mcp/0.2.1 "
    "(https://github.com/akashic-plugins/bangumi-mcp)"
)


class BangumiConfig(BaseModel):
    access_token: SecretStr | None = None
    user_agent: str = Field(default=DEFAULT_USER_AGENT, min_length=1)


class BangumiPlugin(Plugin):
    api_version = 2
    name = "bangumi"
    version = "0.2.1"
    desc = "查询 Bangumi 收藏列表，并安全更新收藏和动画观看进度"
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
