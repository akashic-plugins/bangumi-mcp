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
from collections.abc import Callable, Mapping
from pathlib import Path

from .bangumi._host_contract import CredentialRef
from .bangumi.settings import (
    DEFAULT_USER_AGENT,
    BangumiConfigError,
    SaveCredential,
    build_config_values,
    parse_settings,
    upgrade_legacy_config,
)

ACCESS_TOKEN_ENV = ("AKASHIC_BANGUMI_ACCESS_TOKEN", "BANGUMI_ACCESS_TOKEN")
ANILIST_TOKEN_ENV = ("AKASHIC_BANGUMI_ANILIST_TOKEN", "BANGUMI_ANILIST_TOKEN")
ENABLED_ENV = "AKASHIC_BANGUMI_ANIME_PUSH_ENABLED"
BEFORE_MINUTES_ENV = "AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES"
TIMEZONE_ENV = "AKASHIC_BANGUMI_DISPLAY_TIMEZONE"
USER_AGENT_ENV = "AKASHIC_BANGUMI_USER_AGENT"

# 与 plugin.py 顶层字面量保持一致；配置命令运行在宿主进程内，不 import 插件入口。
PLUGIN_NAME = "bangumi"
PLUGIN_ID = "bangumi@github"

SaveConfig = Callable[[Mapping[str, object]], None]
UpgradeConfig = Callable[
    [Callable[[bytes], Mapping[str, object]]],
    object,
]

_TRUE = {"1", "true", "yes", "on"}


def main(
    arguments: tuple[str, ...] | list[str] = (),
    *,
    workspace: Path | None = None,
    config_path: Path | None = None,
) -> int:
    """运行插件配置命令；返回进程退出码。

    签名对齐宿主的 ``invoke_plugin_command``：``function(arguments,
    workspace=…, config_path=…)``，且返回值必须是 ``int``。
    ``workspace`` 由宿主校验后传入，数据目录从它推导。
    宿主不再向配置命令注入数据目录环境变量，也不经由 ``main.py setup``。
    """

    del config_path
    args = list(arguments)
    if args[:1] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if args and args != ["--upgrade"]:
        raise BangumiConfigError(f"未知参数: {args}")

    from agent.plugin_composition.config_input import (
        load_config,
        save_config,
        save_credential,
        upgrade_config,
    )

    data_dir = _data_dir(workspace)

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
    _verify_written_input(data_dir, load_config)
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


def _verify_written_input(
    data_dir: Path,
    load_config: Callable[[Path], tuple[dict[str, object], str]],
) -> None:
    """写完立即回读，确认宿主能解析出本插件认识的固定输入。

    `config_input` 是宿主的内部写盘实现，没有对外兼容承诺；这里不假设写入
    一定可读，发现格式漂移就明确失败，而不是留给下一次插件加载时报错。
    """

    parsed, _ = load_config(data_dir)
    try:
        _ = parse_settings(parsed)
    except BangumiConfigError as error:
        raise BangumiConfigError(
            f"写入的固定配置无法被插件解析: {error}"
        ) from error


def _data_dir(workspace: Path | None) -> Path:
    """由宿主 workspace 推导插件数据目录：``plugin-data/<name>-<marketplace>``。

    与 ``agent/plugins/manifest.py:workspace_plugin_data_dir`` 同一规则；
    市场名从插件身份后缀取，无后缀时用 ``builtin``。
    """

    if workspace is None:
        raise BangumiConfigError("配置命令缺少 workspace 参数")
    _, _, marketplace = PLUGIN_ID.partition("@")
    return (
        workspace / "plugin-data" / f"{PLUGIN_NAME}-{marketplace or 'builtin'}"
    ).resolve()


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
