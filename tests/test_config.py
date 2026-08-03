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
        "akashic-plugins/bangumi-mcp/0.3.0 "
        "(https://github.com/akashic-plugins/bangumi-mcp)"
    )
    assert "private-value" not in repr(config)


def test_missing_token_fails_without_echoing_file_contents(tmp_path) -> None:
    (tmp_path / "config.local.toml").write_text(
        'user_agent = "safe-agent"\n',
        encoding="utf-8",
    )

    with pytest.raises(BangumiConfigError, match="access_token") as caught:
        load_runtime_config(tmp_path)

    assert "safe-agent" not in str(caught.value)
