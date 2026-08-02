from __future__ import annotations

from typing import Any

import pytest

from src.client import API_BASE_URL, BangumiApiError, BangumiClient
from src.config import BangumiRuntimeConfig


class FakeResponse:
    def __init__(self, status_code: int, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def client(session: FakeSession, token: str = "secret-token") -> BangumiClient:
    return BangumiClient(
        BangumiRuntimeConfig(
            access_token=token,
            user_agent="lfegg/bangumi-mcp/0.1.0 (https://example.test)",
        ),
        session=session,
    )


def test_status_write_only_sends_collection_type() -> None:
    session = FakeSession(FakeResponse(204))

    client(session).set_collection_type(42, 3)

    assert session.calls == [
        {
            "method": "POST",
            "url": f"{API_BASE_URL}/v0/users/-/collections/42",
            "headers": {
                "Accept": "application/json",
                "Authorization": "Bearer secret-token",
                "User-Agent": "lfegg/bangumi-mcp/0.1.0 (https://example.test)",
            },
            "params": None,
            "json": {"type": 3},
            "timeout": (3.05, 15.0),
        }
    ]
    assert "ep_status" not in session.calls[0]["json"]


def test_episode_write_uses_explicit_episode_ids() -> None:
    session = FakeSession(FakeResponse(204))

    client(session).set_episode_collections(42, [1001, 1002])

    assert session.calls[0]["method"] == "PATCH"
    assert session.calls[0]["url"].endswith(
        "/v0/users/-/collections/42/episodes"
    )
    assert session.calls[0]["json"] == {
        "episode_id": [1001, 1002],
        "type": 2,
    }


def test_episode_collections_are_fully_paginated() -> None:
    session = FakeSession(
        FakeResponse(200, {"total": 2, "data": [{"episode": {"id": 1}}]}),
        FakeResponse(200, {"total": 2, "data": [{"episode": {"id": 2}}]}),
    )

    result = client(session).list_episode_collections(42)

    assert [item["episode"]["id"] for item in result] == [1, 2]
    assert [call["params"]["offset"] for call in session.calls] == [0, 1]
    assert all(call["params"]["episode_type"] == 0 for call in session.calls)


def test_collection_404_is_a_distinct_not_collected_result() -> None:
    session = FakeSession(FakeResponse(404, {"title": "not found"}))

    assert client(session).get_collection("tester/name", 42) is None
    assert session.calls[0]["url"].endswith(
        "/v0/users/tester%2Fname/collections/42"
    )


def test_api_errors_redact_token_even_if_remote_echoes_it() -> None:
    token = "never-print-this-token"
    session = FakeSession(
        FakeResponse(
            401,
            {
                "title": "Unauthorized",
                "details": f"bad credential {token}",
            },
        )
    )

    with pytest.raises(BangumiApiError) as caught:
        client(session, token).get_me()

    assert token not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
