"""插件配置的固定输入契约与旧 TOML 转换。

V3 底座不读取模块级配置类，也不读旧 ``config.local.toml``。插件自己解释
``config.input.json`` 的固定映射，并用显式 ``configure.py --upgrade`` 转换旧格式。
本模块只做纯解析与纯转换，不读写文件、不接触宿主 SDK；落盘由 ``configure.py``
调用宿主的 ``save_config`` / ``save_credential`` / ``upgrade_config`` 完成。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._host_contract import CredentialRef

__all__ = [
    "ACCESS_TOKEN_KEY",
    "ANILIST_TOKEN_KEY",
    "DEFAULT_USER_AGENT",
    "AnimePushSettings",
    "BangumiConfigError",
    "BangumiRuntimeConfig",
    "BangumiSettings",
    "TokenProvider",
    "build_config_values",
    "parse_legacy_toml",
    "parse_settings",
    "settings_to_config_values",
    "upgrade_legacy_config",
]

DEFAULT_USER_AGENT = (
    "akashic-plugins/bangumi-mcp/0.7.0 "
    "(https://github.com/akashic-plugins/bangumi-mcp)"
)

ACCESS_TOKEN_KEY = "access_token"
ANILIST_TOKEN_KEY = "anilist_token"
_PUSH_KEYS = (
    "enabled",
    "notify_before_minutes",
    "display_timezone",
    "media_id_overrides",
    ANILIST_TOKEN_KEY,
)

TokenProvider = Callable[[], str]


class BangumiConfigError(ValueError):
    """固定输入或旧 TOML 不满足本插件的配置契约。"""


@dataclass(frozen=True)
class AnimePushSettings:
    """放送提醒的纯配置；V3 目前只保留抓取与手动查询，不自动投递。"""

    enabled: bool = False
    notify_before_minutes: int = 0
    display_timezone: str = "Asia/Shanghai"
    anilist_token: CredentialRef | None = field(default=None, repr=False)
    media_id_overrides: Mapping[int, int] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class BangumiSettings:
    """``ctx.config`` 的解析结果；Token 只有不可变引用，没有明文。"""

    access_token: CredentialRef = field(repr=False)
    user_agent: str = DEFAULT_USER_AGENT
    anime_push: AnimePushSettings = field(default_factory=AnimePushSettings)


@dataclass(frozen=True)
class BangumiRuntimeConfig:
    """运行时客户端配置；Token 通过 provider 在请求时取得，不长期持有。"""

    token_provider: TokenProvider = field(repr=False)
    user_agent: str = DEFAULT_USER_AGENT


SaveCredential = Callable[[str], CredentialRef]
Upgrade = Callable[[Callable[[bytes], Mapping[str, object]]], object]

_MISSING = object()


def parse_settings(config: Mapping[str, object]) -> BangumiSettings:
    """把 ``ctx.config`` 固定映射解析成校验后的设置模型。"""

    if not isinstance(config, Mapping):
        raise BangumiConfigError("插件配置必须是字符串键映射")
    allowed = {"access_token", "user_agent", "anime_push"}
    extra = sorted(set(config) - allowed)
    if extra:
        raise BangumiConfigError(f"插件配置包含未知字段: {extra}")

    access_token = _credential_ref(
        config.get(ACCESS_TOKEN_KEY),
        "access_token",
    )
    user_agent = _text(
        config.get("user_agent", DEFAULT_USER_AGENT),
        "user_agent",
    )
    raw_push = config.get("anime_push", _MISSING)
    if raw_push is _MISSING or raw_push is None:
        push = AnimePushSettings()
    elif isinstance(raw_push, Mapping):
        push = _parse_anime_push(raw_push)
    else:
        raise BangumiConfigError("anime_push 必须是映射")
    return BangumiSettings(
        access_token=access_token,
        user_agent=user_agent,
        anime_push=push,
    )


def parse_legacy_toml(content: bytes) -> tuple[dict[str, object], str]:
    """把旧 ``config.local.toml`` 转成固定映射，并单独返回明文 Token。

    返回的映射只保留非秘密字段；调用方用宿主的 ``save_credential`` 把明文
    Token 换成 ``CredentialRef`` 后再写入固定输入。
    """

    import tomllib

    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise BangumiConfigError("旧 config.local.toml 无法解析") from error
    if not isinstance(raw, dict):
        raise BangumiConfigError("旧 config.local.toml 必须是 TOML table")

    token = _text(raw.get(ACCESS_TOKEN_KEY), ACCESS_TOKEN_KEY)

    raw_push = raw.get("anime_push", {})
    if not isinstance(raw_push, dict):
        raise BangumiConfigError("旧 anime_push 必须是 TOML table")
    extra = sorted(set(raw_push) - set(_PUSH_KEYS))
    if extra:
        raise BangumiConfigError(f"旧 anime_push 包含未知字段: {extra}")

    # 旧值在写盘前就归一化并校验，避免把无效值固化进固定输入。
    enabled = _bool_field(raw_push.get("enabled", False), "anime_push.enabled")
    notify_before = _notify_field(raw_push.get("notify_before_minutes", 0))
    timezone = _timezone(raw_push.get("display_timezone", "Asia/Shanghai"))
    overrides = _clean_overrides(raw_push.get("media_id_overrides", {}))
    anilist_token = raw_push.get(ANILIST_TOKEN_KEY)
    if anilist_token is not None and not isinstance(anilist_token, str):
        raise BangumiConfigError(
            f"anime_push.{ANILIST_TOKEN_KEY} 必须是字符串"
        )

    mapping: dict[str, object] = {
        "user_agent": _text(
            raw.get("user_agent", DEFAULT_USER_AGENT),
            "user_agent",
        ),
        "anime_push": {
            "enabled": enabled,
            "notify_before_minutes": notify_before,
            "display_timezone": timezone,
            "media_id_overrides": {
                str(subject_id): media_id
                for subject_id, media_id in overrides.items()
            },
        },
    }
    push_values = mapping["anime_push"]
    assert isinstance(push_values, dict)
    if isinstance(anilist_token, str) and anilist_token.strip():
        push_values[ANILIST_TOKEN_KEY] = _text(
            anilist_token,
            ANILIST_TOKEN_KEY,
        )
    return mapping, token


def build_config_values(
    *,
    access_token_ref: CredentialRef,
    user_agent: str,
    anime_push_enabled: bool = False,
    notify_before_minutes: int = 0,
    display_timezone: str = "Asia/Shanghai",
    anilist_token_ref: CredentialRef | None = None,
    media_id_overrides: Mapping[int, int] | None = None,
) -> dict[str, object]:
    """构造可交给宿主 ``save_config`` 的固定映射（只含引用，不含明文）。"""

    settings = BangumiSettings(
        access_token=access_token_ref,
        user_agent=_text(user_agent, "user_agent"),
        anime_push=AnimePushSettings(
            enabled=bool(anime_push_enabled),
            notify_before_minutes=notify_before_minutes,
            display_timezone=_timezone(display_timezone),
            anilist_token=anilist_token_ref,
            media_id_overrides=MappingProxyType(
                _clean_overrides(media_id_overrides or {})
            ),
        ),
    )
    return settings_to_config_values(settings)


def settings_to_config_values(
    settings: BangumiSettings,
) -> dict[str, object]:
    """把设置模型编码成宿主固定输入映射。"""

    push = settings.anime_push
    values: dict[str, object] = {
        ACCESS_TOKEN_KEY: settings.access_token,
        "user_agent": settings.user_agent,
        "anime_push": {
            "enabled": push.enabled,
            "notify_before_minutes": push.notify_before_minutes,
            "display_timezone": push.display_timezone,
            "media_id_overrides": {
                str(subject_id): media_id
                for subject_id, media_id in push.media_id_overrides.items()
            },
        },
    }
    if push.anilist_token is not None:
        push_values = values["anime_push"]
        assert isinstance(push_values, dict)
        push_values[ANILIST_TOKEN_KEY] = push.anilist_token
    return values


def upgrade_legacy_config(
    *,
    save_credential: SaveCredential,
    upgrade: Upgrade,
) -> None:
    """把旧 TOML 转成固定输入；明文只经 ``save_credential``。

    读取、恢复点与旧文件退役全部由宿主 ``upgrade_config`` 负责，本函数不直接
    写文件、不保留明文。
    """

    def convert(content: bytes) -> Mapping[str, object]:
        mapping, token = parse_legacy_toml(content)
        push = mapping.get("anime_push")
        if isinstance(push, dict):
            raw_anilist = push.pop(ANILIST_TOKEN_KEY, None)
            if isinstance(raw_anilist, str):
                push[ANILIST_TOKEN_KEY] = save_credential(raw_anilist)
        mapping[ACCESS_TOKEN_KEY] = save_credential(token)
        return mapping

    upgrade(convert)


def _parse_anime_push(value: Mapping[str, object]) -> AnimePushSettings:
    extra = sorted(set(value) - set(_PUSH_KEYS))
    if extra:
        raise BangumiConfigError(f"anime_push 包含未知字段: {extra}")

    raw_anilist = value.get(ANILIST_TOKEN_KEY)
    anilist_ref = (
        None
        if raw_anilist is None
        else _credential_ref(raw_anilist, f"anime_push.{ANILIST_TOKEN_KEY}")
    )
    return AnimePushSettings(
        enabled=_bool_field(value.get("enabled", False), "anime_push.enabled"),
        notify_before_minutes=_notify_field(value.get("notify_before_minutes", 0)),
        display_timezone=_timezone(
            value.get("display_timezone", "Asia/Shanghai")
        ),
        anilist_token=anilist_ref,
        media_id_overrides=MappingProxyType(
            _clean_overrides(value.get("media_id_overrides", {}))
        ),
    )


def _bool_field(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise BangumiConfigError(f"{label} 必须是布尔值")
    return value


def _notify_field(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 1440
    ):
        raise BangumiConfigError(
            "anime_push.notify_before_minutes 必须是 0 至 1440 的整数"
        )
    return value


def _credential_ref(value: object, label: str) -> CredentialRef:
    if isinstance(value, CredentialRef):
        return value
    if isinstance(value, str):
        raise BangumiConfigError(
            f"{label} 必须是凭据引用；明文不能写入固定输入"
        )
    if isinstance(value, (tuple, list)) and all(
        isinstance(part, str) and part for part in value
    ):
        return CredentialRef(tuple(value))
    raise BangumiConfigError(f"{label} 必须是凭据引用")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BangumiConfigError(f"{label} 必须是非空字符串")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise BangumiConfigError(f"{label} 不能包含换行")
    return text


def _timezone(value: object) -> str:
    text = _text(value, "anime_push.display_timezone")
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError as error:
        raise BangumiConfigError(
            "anime_push.display_timezone 必须是有效 IANA 时区"
        ) from error
    return text


def _clean_overrides(value: object) -> dict[int, int]:
    if not isinstance(value, Mapping):
        raise BangumiConfigError("media_id_overrides 必须是映射")
    overrides: dict[int, int] = {}
    for raw_subject_id, raw_media_id in value.items():
        if isinstance(raw_media_id, bool):
            raise BangumiConfigError("media_id_overrides 的 ID 必须是正整数")
        subject_id = _positive_id(raw_subject_id, "subject")
        media_id = _positive_id(raw_media_id, "media")
        overrides[subject_id] = media_id
    return overrides


def _positive_id(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise BangumiConfigError(f"media_id_overrides 的 {label} ID 必须是正整数")
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise BangumiConfigError(
            f"media_id_overrides 的 {label} ID 必须是正整数"
        ) from error
    if number <= 0:
        raise BangumiConfigError(f"media_id_overrides 的 {label} ID 必须是正整数")
    return number


