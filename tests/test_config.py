from __future__ import annotations

import pytest

from src.config import BangumiConfigError, load_runtime_config


def test_runtime_config_keeps_token_out_of_repr(tmp_path) -> None:
    (tmp_path / "config.local.toml").write_text(
        'access_token = "private-value"\n',
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path)

    assert config.access_token == "private-value"
    assert config.user_agent == (
        "akashic-plugins/bangumi-mcp/0.5.0 "
        "(https://github.com/akashic-plugins/bangumi-mcp)"
    )
    assert config.anime_push.enabled is False
    assert "private-value" not in repr(config)


def test_missing_token_fails_without_echoing_file_contents(tmp_path) -> None:
    (tmp_path / "config.local.toml").write_text(
        'user_agent = "safe-agent"\n',
        encoding="utf-8",
    )

    with pytest.raises(BangumiConfigError, match="access_token") as caught:
        load_runtime_config(tmp_path)

    assert "safe-agent" not in str(caught.value)


def test_runtime_config_loads_private_anime_push_settings(tmp_path) -> None:
    (tmp_path / "config.local.toml").write_text(
        """
access_token = "bangumi-private"

[anime_push]
enabled = true
notify_before_minutes = 15
display_timezone = "Asia/Tokyo"
anilist_token = "anilist-private"

[anime_push.media_id_overrides]
"42" = 154587
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path)

    assert config.anime_push.enabled is True
    assert config.anime_push.notify_before_minutes == 15
    assert config.anime_push.display_timezone == "Asia/Tokyo"
    assert config.anime_push.anilist_token == "anilist-private"
    assert dict(config.anime_push.media_id_overrides) == {42: 154587}
    assert "bangumi-private" not in repr(config)
    assert "anilist-private" not in repr(config)


@pytest.mark.parametrize("value", [True, -1, 1441, 1.5, "15"])
def test_runtime_config_rejects_invalid_notify_before(tmp_path, value) -> None:
    rendered = str(value).lower() if isinstance(value, bool) else repr(value)
    (tmp_path / "config.local.toml").write_text(
        f'access_token = "private"\n[anime_push]\nnotify_before_minutes = {rendered}\n',
        encoding="utf-8",
    )

    with pytest.raises(BangumiConfigError, match="notify_before_minutes"):
        load_runtime_config(tmp_path)
