from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_USER_AGENT = (
    "akashic-plugins/bangumi-mcp/0.3.0 "
    "(https://github.com/akashic-plugins/bangumi-mcp)"
)


class BangumiConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class BangumiRuntimeConfig:
    access_token: str = field(repr=False)
    user_agent: str = DEFAULT_USER_AGENT


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

    return BangumiRuntimeConfig(access_token=token, user_agent=user_agent)
