from __future__ import annotations

from typing import Any

import pytest

from src.anilist import (
    ANILIST_API_URL,
    AniListApiError,
    AniListClient,
    AniListNotFoundError,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def client(session: FakeSession, token: str | None = None) -> AniListClient:
    return AniListClient(
        user_agent="akashic-plugins/bangumi-mcp/0.5.0 (https://example.test)",
        token=token,
        session=session,
    )


def test_search_uses_graphql_variables_and_fixed_candidate_page() -> None:
    media = {"id": 154587, "type": "ANIME"}
    session = FakeSession(
        FakeResponse(200, {"data": {"Page": {"media": [media]}}})
    )

    result = client(session).search_anime("葬送のフリーレン")

    assert result == [media]
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == ANILIST_API_URL
    assert call["json"]["operationName"] == "SearchAnime"
    assert call["json"]["variables"] == {
        "search": "葬送のフリーレン",
        "page": 1,
        "perPage": 25,
    }
    assert "葬送のフリーレン" not in call["json"]["query"]
    assert "Authorization" not in call["headers"]


def test_media_by_id_uses_optional_private_token() -> None:
    token = "anilist-private"
    media = {"id": 154587, "type": "ANIME"}
    session = FakeSession(FakeResponse(200, {"data": {"Media": media}}))

    result = client(session, token).get_anime(154587)

    assert result == media
    call = session.calls[0]
    assert call["json"]["operationName"] == "AnimeById"
    assert call["json"]["variables"] == {"id": 154587}
    assert call["headers"]["Authorization"] == f"Bearer {token}"
    assert token not in repr(client(session, token))


def test_http_200_graphql_errors_are_failures_without_remote_details() -> None:
    remote_detail = "do-not-copy-remote-detail"
    session = FakeSession(
        FakeResponse(200, {"errors": [{"message": remote_detail}], "data": {}})
    )

    with pytest.raises(AniListApiError, match="GraphQL") as caught:
        client(session).get_anime(154587)

    assert remote_detail not in str(caught.value)


def test_rate_limit_records_retry_after_without_response_body() -> None:
    session = FakeSession(
        FakeResponse(
            429,
            {"errors": [{"message": "ignored"}]},
            headers={"Retry-After": "90"},
        )
    )

    with pytest.raises(AniListApiError) as caught:
        client(session).get_anime(154587)

    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 90
    assert "ignored" not in str(caught.value)


def test_missing_media_is_distinct_from_temporary_graphql_failure() -> None:
    session = FakeSession(FakeResponse(200, {"data": {"Media": None}}))

    with pytest.raises(AniListNotFoundError):
        client(session).get_anime(999999)
