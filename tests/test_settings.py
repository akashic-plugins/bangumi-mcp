from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Mapping
from pathlib import Path

import pytest
from bangumi_test_plugin.bangumi._host_contract import CredentialRef
from bangumi_test_plugin.bangumi.settings import (
    DEFAULT_USER_AGENT,
    BangumiConfigError,
    build_config_values,
    parse_legacy_toml,
    parse_settings,
    settings_to_config_values,
)

LEGACY_TOML = b"""
access_token = "legacy-bangumi-token"
user_agent = "custom-agent/1.0"

[anime_push]
enabled = true
notify_before_minutes = 15
display_timezone = "Asia/Tokyo"
anilist_token = "legacy-anilist-token"

[anime_push.media_id_overrides]
"501963" = 123456
"""


def test_parse_settings_accepts_structured_credential_refs() -> None:
    settings = parse_settings(
        {
            "access_token": CredentialRef(("owner-token", "revision")),
            "user_agent": "custom-agent",
            "anime_push": {
                "enabled": True,
                "notify_before_minutes": 30,
                "display_timezone": "Asia/Tokyo",
                "anilist_token": CredentialRef(("anilist", "revision")),
                "media_id_overrides": {"501963": 123456},
            },
        }
    )

    assert settings.access_token == CredentialRef(("owner-token", "revision"))
    assert settings.user_agent == "custom-agent"
    assert settings.anime_push.enabled is True
    assert settings.anime_push.notify_before_minutes == 30
    assert settings.anime_push.display_timezone == "Asia/Tokyo"
    assert settings.anime_push.anilist_token == CredentialRef(
        ("anilist", "revision")
    )
    assert dict(settings.anime_push.media_id_overrides) == {501963: 123456}


def test_parse_settings_rejects_plaintext_secret() -> None:
    with pytest.raises(BangumiConfigError, match="凭据引用"):
        parse_settings({"access_token": "plain-text-token"})


def test_parse_settings_rejects_unknown_fields() -> None:
    with pytest.raises(BangumiConfigError, match="未知字段"):
        parse_settings(
            {
                "access_token": CredentialRef(("a", "b")),
                "legacy_field": 1,
            }
        )


def test_parse_settings_defaults_are_safe() -> None:
    settings = parse_settings({"access_token": CredentialRef(("a", "b"))})

    assert settings.user_agent == DEFAULT_USER_AGENT
    assert settings.anime_push.enabled is False
    assert settings.anime_push.notify_before_minutes == 0
    assert settings.anime_push.display_timezone == "Asia/Shanghai"
    assert settings.anime_push.anilist_token is None


def test_parse_settings_validates_anime_push_bounds() -> None:
    with pytest.raises(BangumiConfigError, match="notify_before_minutes"):
        parse_settings(
            {
                "access_token": CredentialRef(("a", "b")),
                "anime_push": {"notify_before_minutes": 1441},
            }
        )
    with pytest.raises(BangumiConfigError, match="display_timezone"):
        parse_settings(
            {
                "access_token": CredentialRef(("a", "b")),
                "anime_push": {"display_timezone": "Mars/Olympus"},
            }
        )
    with pytest.raises(BangumiConfigError, match="media_id_overrides"):
        parse_settings(
            {
                "access_token": CredentialRef(("a", "b")),
                "anime_push": {"media_id_overrides": {"0": 1}},
            }
        )


def test_settings_repr_does_not_leak_credentials() -> None:
    settings = parse_settings(
        {
            "access_token": CredentialRef(("secret-token-id", "secret-revision")),
            "anime_push": {
                "anilist_token": CredentialRef(("anilist-id", "anilist-revision"))
            },
        }
    )

    assert "anilist-id" not in repr(settings)
    assert repr(settings.anime_push) is not None


def test_parse_legacy_toml_separates_secrets_from_config() -> None:
    mapping, token = parse_legacy_toml(LEGACY_TOML)

    assert token == "legacy-bangumi-token"
    assert "access_token" not in mapping
    assert mapping["user_agent"] == "custom-agent/1.0"
    push = mapping["anime_push"]
    assert isinstance(push, dict)
    assert push["enabled"] is True
    assert push["notify_before_minutes"] == 15
    assert push["display_timezone"] == "Asia/Tokyo"
    assert push["anilist_token"] == "legacy-anilist-token"
    assert push["media_id_overrides"] == {"501963": 123456}


def test_parse_legacy_toml_requires_access_token() -> None:
    with pytest.raises(BangumiConfigError, match="access_token"):
        parse_legacy_toml(b'user_agent = "x"\n')


def test_parse_legacy_toml_rejects_invalid_values_before_persisting() -> None:
    with pytest.raises(BangumiConfigError, match="notify_before_minutes"):
        parse_legacy_toml(
            b'access_token = "t"\n[anime_push]\nnotify_before_minutes = 9999\n'
        )
    with pytest.raises(BangumiConfigError, match="display_timezone"):
        parse_legacy_toml(
            b'access_token = "t"\n[anime_push]\n'
            b'display_timezone = "Mars/Olympus"\n'
        )
    with pytest.raises(BangumiConfigError, match="enabled"):
        parse_legacy_toml(b'access_token = "t"\n[anime_push]\nenabled = "yes"\n')
    with pytest.raises(BangumiConfigError, match="未知字段"):
        parse_legacy_toml(
            b'access_token = "t"\n[anime_push]\nlegacy_field = 1\n'
        )


def test_build_config_values_round_trips_through_parse_settings() -> None:
    values = build_config_values(
        access_token_ref=CredentialRef(("id", "rev")),
        user_agent="custom-agent",
        anime_push_enabled=True,
        notify_before_minutes=10,
        display_timezone="Asia/Tokyo",
        anilist_token_ref=CredentialRef(("aid", "arev")),
        media_id_overrides={501963: 123456},
    )

    assert not any(
        isinstance(value, str) and value in {"id", "rev"} for value in values
    )
    parsed = parse_settings(values)
    assert parsed.access_token == CredentialRef(("id", "rev"))
    assert parsed.user_agent == "custom-agent"
    assert parsed.anime_push.enabled is True
    assert parsed.anime_push.notify_before_minutes == 10
    assert parsed.anime_push.display_timezone == "Asia/Tokyo"
    assert parsed.anime_push.anilist_token == CredentialRef(("aid", "arev"))
    assert dict(parsed.anime_push.media_id_overrides) == {501963: 123456}


def test_settings_to_config_values_omits_absent_anilist_token() -> None:
    settings = parse_settings({"access_token": CredentialRef(("a", "b"))})

    values = settings_to_config_values(settings)

    push = values["anime_push"]
    assert isinstance(push, dict)
    assert "anilist_token" not in push
    assert push["media_id_overrides"] == {}


def _load_configure_module() -> types.ModuleType:
    """从伪包里导入 configure，让它的相对导入（`.bangumi.…`）成立。"""

    module = importlib.import_module("bangumi_test_plugin.configure")
    return module


class _HostRecorder:
    """记录宿主 config_input 调用，避免触碰真实凭据目录。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.credentials: list[str] = []
        self.saved: list[Mapping[str, object]] = []
        self.upgraded = 0

    def save_credential(self, data_dir: Path, value: str) -> CredentialRef:
        assert data_dir == self.data_dir
        self.credentials.append(value)
        return CredentialRef((f"id-{len(self.credentials)}", "rev"))

    def save_config(self, data_dir: Path, values: Mapping[str, object]) -> None:
        assert data_dir == self.data_dir
        self.saved.append(dict(values))

    def load_config(self, data_dir: Path) -> tuple[dict[str, object], str]:
        assert data_dir == self.data_dir
        values = self.saved[-1] if self.saved else {}
        return dict(values), "revision"

    def upgrade_config(self, data_dir: Path, convert: object) -> Path:
        assert data_dir == self.data_dir
        self.upgraded += 1
        values = convert(LEGACY_TOML)  # type: ignore[operator]
        self.saved.append(dict(values))
        return data_dir.parent / "backup"


def _install_fake_host(monkeypatch: pytest.MonkeyPatch, recorder: _HostRecorder) -> None:
    agent = types.ModuleType("agent")
    composition = types.ModuleType("agent.plugin_composition")
    config_input = types.ModuleType("agent.plugin_composition.config_input")
    config_input.save_credential = recorder.save_credential
    config_input.save_config = recorder.save_config
    config_input.upgrade_config = recorder.upgrade_config
    # configure 写完会回读自校验（见 configure._verify_written_input）。
    config_input.load_config = recorder.load_config
    composition.config_input = config_input
    agent.plugin_composition = composition
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.plugin_composition", composition)
    monkeypatch.setitem(
        sys.modules,
        "agent.plugin_composition.config_input",
        config_input,
    )


def _plugin_data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "plugin-data" / "bangumi-github"
    directory.mkdir(parents=True)
    return directory


def _run_configure(
    arguments: list[str],
    *,
    workspace: Path,
) -> int:
    """按宿主的命令签名调用 configure（见 plugin-contract-refresh 4.6）。

    `main.py setup` 不再注入 `AKASHIC_PLUGIN_DATA_DIR`，数据目录由宿主的
    `workspace` 参数推导为 `plugin-data/<name>-<marketplace>`。
    旧实现的 `main(argv)` 只接收参数列表；目标实现必须额外接收 workspace。
    """

    module = _load_configure_module()
    return module.main(arguments, workspace=workspace, config_path=workspace / "config.toml")


def test_configure_writes_only_credential_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _plugin_data_dir(tmp_path)
    recorder = _HostRecorder(data_dir)
    _install_fake_host(monkeypatch, recorder)
    monkeypatch.setenv("AKASHIC_BANGUMI_ACCESS_TOKEN", "new-bangumi-token")
    monkeypatch.setenv("AKASHIC_BANGUMI_ANILIST_TOKEN", "new-anilist-token")
    monkeypatch.setenv("AKASHIC_BANGUMI_ANIME_PUSH_ENABLED", "true")
    monkeypatch.setenv("AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES", "20")

    assert _run_configure([], workspace=tmp_path) == 0

    assert recorder.credentials == ["new-bangumi-token", "new-anilist-token"]
    assert len(recorder.saved) == 1
    values = recorder.saved[0]
    assert "new-bangumi-token" not in repr(values)
    assert "new-anilist-token" not in repr(values)
    parsed = parse_settings(values)
    assert parsed.anime_push.enabled is True
    assert parsed.anime_push.notify_before_minutes == 20
    assert parsed.access_token == CredentialRef(("id-1", "rev"))


def test_configure_requires_access_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _plugin_data_dir(tmp_path)
    recorder = _HostRecorder(data_dir)
    _install_fake_host(monkeypatch, recorder)
    monkeypatch.delenv("AKASHIC_BANGUMI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BANGUMI_ACCESS_TOKEN", raising=False)

    with pytest.raises(BangumiConfigError, match="Access Token"):
        _run_configure([], workspace=tmp_path)

    assert recorder.saved == []


def test_configure_upgrade_converts_legacy_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _plugin_data_dir(tmp_path)
    recorder = _HostRecorder(data_dir)
    _install_fake_host(monkeypatch, recorder)

    assert _run_configure(["--upgrade"], workspace=tmp_path) == 0

    assert recorder.upgraded == 1
    assert recorder.credentials == ["legacy-anilist-token", "legacy-bangumi-token"]
    values = recorder.saved[0]
    assert "legacy-bangumi-token" not in repr(values)
    assert "legacy-anilist-token" not in repr(values)
    parsed = parse_settings(values)
    assert parsed.anime_push.enabled is True
    assert parsed.anime_push.anilist_token == CredentialRef(("id-1", "rev"))
    assert parsed.access_token == CredentialRef(("id-2", "rev"))


def test_configure_rejects_unknown_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _plugin_data_dir(tmp_path)
    recorder = _HostRecorder(data_dir)
    _install_fake_host(monkeypatch, recorder)

    with pytest.raises(BangumiConfigError, match="未知参数"):
        _run_configure(["--access-token", "leak"], workspace=tmp_path)


def test_configure_plain_mode_reads_no_legacy_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认普通模式只写固定输入，不读取也不写旧 config.local.toml。"""

    data_dir = _plugin_data_dir(tmp_path)
    legacy = data_dir / "config.local.toml"
    legacy.write_text('access_token = "old"\n', encoding="utf-8")
    recorder = _HostRecorder(data_dir)
    _install_fake_host(monkeypatch, recorder)
    monkeypatch.setenv("AKASHIC_BANGUMI_ACCESS_TOKEN", "new-token")

    assert _run_configure([], workspace=tmp_path) == 0

    assert recorder.upgraded == 0
    assert legacy.read_text(encoding="utf-8") == 'access_token = "old"\n'


def test_docs_teach_the_configure_command_instead_of_plaintext_files() -> None:
    """README 与 Skill 必须教用户走插件自有配置命令，而不是手写明文配置。

    宿主 `main.py setup` 已不再发现或执行 `configure.py`（见
    docs/design/plugin-contract-refresh.md 4.6），文档必须指向 `bangumi-configure`。
    """

    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text(encoding="utf-8")
    skill = (repo / "skills/bangumi/SKILL.md").read_text(encoding="utf-8")

    assert "bangumi-configure" in readme
    assert "config.local.toml" not in skill
    for env_name in (
        "AKASHIC_BANGUMI_ACCESS_TOKEN",
        "AKASHIC_BANGUMI_ANILIST_TOKEN",
        "AKASHIC_BANGUMI_ANIME_PUSH_ENABLED",
        "AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES",
        "AKASHIC_BANGUMI_DISPLAY_TIMEZONE",
        "AKASHIC_BANGUMI_USER_AGENT",
    ):
        assert env_name in readme, env_name
    assert "mcp/requirements.txt" not in readme
    assert "bangumi-configure" in skill
