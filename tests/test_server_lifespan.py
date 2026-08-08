from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import src.server as server_module


def test_enabled_anime_push_lifespan_starts_and_stops_coordinator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "config.local.toml").write_text(
        """
access_token = "bangumi-placeholder"

[anime_push]
enabled = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    instances: list[FakeCoordinator] = []

    class FakeCoordinator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.started = False
            self.stopped = False
            instances.append(self)

        async def run(self) -> None:
            self.started = True
            try:
                await asyncio.Future()
            finally:
                self.stopped = True

    monkeypatch.setattr(server_module, "AnimeUpdateCoordinator", FakeCoordinator)
    server = server_module.create_mcp_server(tmp_path)

    async def exercise_lifespan() -> None:
        async with server._mcp_server.lifespan(server._mcp_server):
            await asyncio.sleep(0)
            assert instances[0].started is True

    asyncio.run(exercise_lifespan())

    assert instances[0].stopped is True
    assert (tmp_path / "anime_updates.db").is_file()
