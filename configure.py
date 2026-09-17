#!/usr/bin/env python3
"""Bangumi 插件的显式配置程序。

只有用户显式运行 ``main.py setup`` 时，宿主才会在插件根目录下用固定 Python
环境执行本文件；正常加载、候选装配和换代都不运行它。

用法::

    python configure.py              # 从环境变量写入固定配置
    python configure.py --upgrade    # 把旧 config.local.toml 转成固定配置

普通模式读取下列环境变量，不接受命令行明文凭据（命令行会进入进程列表）：

- ``AKASHIC_BANGUMI_ACCESS_TOKEN``（必需，回退 ``BANGUMI_ACCESS_TOKEN``）
- ``AKASHIC_BANGUMI_ANILIST_TOKEN``（可选，回退 ``BANGUMI_ANILIST_TOKEN``）
- ``AKASHIC_BANGUMI_ANIME_PUSH_ENABLED``（可选，"1"/"true" 表示启用）
- ``AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES``（可选，0 至 1440）
- ``AKASHIC_BANGUMI_DISPLAY_TIMEZONE``（可选，IANA 时区）
- ``AKASHIC_BANGUMI_USER_AGENT``（可选）
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from bangumi._host_contract import CredentialRef
from bangumi.settings import (
    DEFAULT_USER_AGENT,
    BangumiConfigError,
    SaveCredential,
    build_config_values,
    upgrade_legacy_config,
)

ACCESS_TOKEN_ENV = ("AKASHIC_BANGUMI_ACCESS_TOKEN", "BANGUMI_ACCESS_TOKEN")
ANILIST_TOKEN_ENV = ("AKASHIC_BANGUMI_ANILIST_TOKEN", "BANGUMI_ANILIST_TOKEN")
ENABLED_ENV = "AKASHIC_BANGUMI_ANIME_PUSH_ENABLED"
BEFORE_MINUTES_ENV = "AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES"
TIMEZONE_ENV = "AKASHIC_BANGUMI_DISPLAY_TIMEZONE"
USER_AGENT_ENV = "AKASHIC_BANGUMI_USER_AGENT"

SaveConfig = Callable[[Mapping[str, object]], None]
UpgradeConfig = Callable[
    [Callable[[bytes], Mapping[str, object]]],
    object,
]

_TRUE = {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    """运行插件配置程序；返回进程退出码。"""

    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if args and args != ["--upgrade"]:
        raise BangumiConfigError(f"未知参数: {args}")

    from agent.plugin_composition.config_input import (
        save_config,
        save_credential,
        upgrade_config,
    )

    data_dir = _data_dir()

    def save_credential_value(value: str) -> CredentialRef:
        return save_credential(data_dir, value)

    def save_config_values(values: Mapping[str, object]) -> None:
        save_config(data_dir, values)

    if args == ["--upgrade"]:
        upgrade_legacy_config(
            save_credential=save_credential_value,
            upgrade=lambda convert: upgrade_config(data_dir, convert),
        )
        print(f"已完成旧配置升级: {data_dir}")
        return 0

    _write_new_config(
        env=os.environ,
        save_credential=save_credential_value,
        save_config=save_config_values,
    )
    print(f"已写入固定配置: {data_dir}")
    return 0


def _write_new_config(
    *,
    env: Mapping[str, str],
    save_credential: SaveCredential,
    save_config: SaveConfig,
) -> None:
    access_token = _first_env(env, ACCESS_TOKEN_ENV)
    if access_token is None:
        raise BangumiConfigError(
            "缺少 Bangumi Access Token；请设置 " + " 或 ".join(ACCESS_TOKEN_ENV)
        )
    anilist_token = _first_env(env, ANILIST_TOKEN_ENV)

    access_ref = save_credential(access_token)
    anilist_ref = (
        None if anilist_token is None else save_credential(anilist_token)
    )
    values = build_config_values(
        access_token_ref=access_ref,
        user_agent=env.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT,
        anime_push_enabled=_bool_env(env.get(ENABLED_ENV), default=False),
        notify_before_minutes=_int_env(env.get(BEFORE_MINUTES_ENV), default=0),
        display_timezone=env.get(TIMEZONE_ENV, "").strip() or "Asia/Shanghai",
        anilist_token_ref=anilist_ref,
    )
    save_config(values)


def _data_dir() -> Path:
    raw = os.environ.get("AKASHIC_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise BangumiConfigError("配置程序必须由 main.py setup 提供数据目录")
    return Path(raw).expanduser()


def _first_env(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def _bool_env(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE


def _int_env(value: str | None, *, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        number = int(value.strip())
    except ValueError as error:
        raise BangumiConfigError("提前提醒分钟数必须是整数") from error
    if not 0 <= number <= 1440:
        raise BangumiConfigError("提前提醒分钟数必须是 0 至 1440 的整数")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
