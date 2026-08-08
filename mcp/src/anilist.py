from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests


ANILIST_API_URL = "https://graphql.anilist.co"
REQUEST_TIMEOUT = (3.05, 15.0)

SEARCH_ANIME_QUERY = """
query SearchAnime($search: String!, $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo {
      total
      currentPage
      lastPage
      hasNextPage
    }
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      type
      format
      status
      episodes
      season
      seasonYear
      startDate { year month day }
      title { romaji english native }
      nextAiringEpisode { episode airingAt }
    }
  }
}
""".strip()

ANIME_BY_ID_QUERY = """
query AnimeById($id: Int!) {
  Media(id: $id, type: ANIME) {
    id
    type
    format
    status
    episodes
    season
    seasonYear
    startDate { year month day }
    title { romaji english native }
    nextAiringEpisode { episode airingAt }
  }
}
""".strip()


class AniListApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class AniListNotFoundError(AniListApiError):
    pass


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response: ...


class AniListClient:
    def __init__(
        self,
        *,
        user_agent: str,
        token: str | None = None,
        session: HttpTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._token = token
        self._session: HttpTransport = session or requests

    def search_anime(self, search: str) -> list[dict[str, Any]]:
        clean = search.strip()
        if not clean:
            raise ValueError("AniList search 不能为空")
        data = self._graphql(
            "SearchAnime",
            SEARCH_ANIME_QUERY,
            {"search": clean, "page": 1, "perPage": 25},
        )
        page = _object(data.get("Page"), "Page")
        media = page.get("media")
        if not isinstance(media, list):
            raise AniListApiError("AniList Page.media 不是数组")
        return [_object(item, "media item") for item in media]

    def get_anime(self, media_id: int) -> dict[str, Any]:
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id <= 0:
            raise ValueError("AniList media ID 必须是正整数")
        data = self._graphql(
            "AnimeById",
            ANIME_BY_ID_QUERY,
            {"id": media_id},
        )
        media = data.get("Media")
        if media is None:
            raise AniListNotFoundError("AniList Media 不存在")
        return _object(media, "Media")

    def _graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, object],
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = self._session.request(
                "POST",
                ANILIST_API_URL,
                headers=headers,
                json={
                    "operationName": operation_name,
                    "query": query,
                    "variables": variables,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.Timeout:
            raise AniListApiError("AniList API 请求超时") from None
        except requests.ConnectionError:
            raise AniListApiError("无法连接 AniList API") from None
        except requests.RequestException:
            raise AniListApiError("AniList API 请求失败") from None

        if response.status_code != 200:
            raise AniListApiError(
                f"AniList API 返回 HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after_seconds=_retry_after_seconds(response),
            )
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            raise AniListApiError("AniList API 返回了无效 JSON") from None
        root = _object(payload, "response")
        errors = root.get("errors")
        if errors:
            raise AniListApiError("AniList GraphQL 返回 errors")
        return _object(root.get("data"), "data")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AniListApiError(f"AniList {label} 不是对象")
    return value


def _retry_after_seconds(response: requests.Response) -> float | None:
    headers = getattr(response, "headers", {})
    value = headers.get("Retry-After") if isinstance(headers, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        return None
    clean = value.strip()
    try:
        seconds = float(clean)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(clean)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0 or seconds > 86_400:
        return None
    return seconds
