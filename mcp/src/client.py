from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import quote

import requests

from .config import BangumiRuntimeConfig


API_BASE_URL = "https://api.bgm.tv"
REQUEST_TIMEOUT = (3.05, 15.0)


class BangumiApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response: ...


class BangumiClient:
    def __init__(
        self,
        config: BangumiRuntimeConfig,
        *,
        session: HttpTransport | None = None,
    ) -> None:
        self._token = config.access_token
        self._user_agent = config.user_agent
        self._session: HttpTransport = session or requests

    def get_me(self) -> dict[str, Any]:
        return self._object(self._request_json("GET", "/v0/me"), "当前用户")

    def get_subject(self, subject_id: int) -> dict[str, Any]:
        return self._object(
            self._request_json("GET", f"/v0/subjects/{subject_id}"),
            "条目",
        )

    def get_collection(
        self,
        username: str,
        subject_id: int,
    ) -> dict[str, Any] | None:
        encoded_username = quote(username, safe="")
        payload = self._request_json(
            "GET",
            f"/v0/users/{encoded_username}/collections/{subject_id}",
            allow_not_found=True,
        )
        if payload is None:
            return None
        return self._object(payload, "收藏")

    def list_episode_collections(
        self,
        subject_id: int,
        *,
        episode_type: int = 0,
    ) -> list[dict[str, Any]]:
        """分页读取一个条目的章节收藏，拒绝不完整分页。"""

        offset = 0
        limit = 100
        result: list[dict[str, Any]] = []
        while True:
            payload = self._object(
                self._request_json(
                    "GET",
                    f"/v0/users/-/collections/{subject_id}/episodes",
                    params={
                        "offset": offset,
                        "limit": limit,
                        "episode_type": episode_type,
                    },
                ),
                "章节收藏分页",
            )
            total = payload.get("total")
            data = payload.get("data")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise BangumiApiError("Bangumi 章节收藏 total 无效")
            if not isinstance(data, list):
                raise BangumiApiError("Bangumi 章节收藏 data 不是数组")
            page = [self._object(item, "章节收藏条目") for item in data]
            result.extend(page)
            offset += len(page)
            if offset >= total:
                return result
            if not page:
                raise BangumiApiError("Bangumi 章节收藏分页提前结束")

    def set_collection_type(self, subject_id: int, collection_type: int) -> None:
        self._request_json(
            "POST",
            f"/v0/users/-/collections/{subject_id}",
            json_body={"type": collection_type},
            expected_status=(204,),
        )

    def set_episode_collections(
        self,
        subject_id: int,
        episode_ids: Sequence[int],
        *,
        collection_type: int = 2,
    ) -> None:
        ids = list(episode_ids)
        if not ids:
            raise ValueError("episode_ids 不能为空")
        self._request_json(
            "PATCH",
            f"/v0/users/-/collections/{subject_id}/episodes",
            json_body={"episode_id": ids, "type": collection_type},
            expected_status=(204,),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        expected_status: tuple[int, ...] = (200,),
        allow_not_found: bool = False,
    ) -> object:
        if not path.startswith("/v0/"):
            raise ValueError(f"Bangumi API path 不受信任: {path}")
        try:
            response = self._session.request(
                method,
                f"{API_BASE_URL}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": self._user_agent,
                },
                params=dict(params) if params is not None else None,
                json=dict(json_body) if json_body is not None else None,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.Timeout:
            raise BangumiApiError("Bangumi API 请求超时，远端结果未知") from None
        except requests.ConnectionError:
            raise BangumiApiError("无法连接 Bangumi API") from None
        except requests.RequestException:
            raise BangumiApiError("Bangumi API 请求失败") from None

        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code not in expected_status:
            detail = self._error_detail(response)
            raise BangumiApiError(
                f"Bangumi API 返回 HTTP {response.status_code}{detail}",
                status_code=response.status_code,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except requests.JSONDecodeError:
            raise BangumiApiError("Bangumi API 返回了无效 JSON") from None

    def _error_detail(self, response: requests.Response) -> str:
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            return ""
        if not isinstance(payload, Mapping):
            return ""
        parts: list[str] = []
        for key in ("title", "description", "details"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                safe = value.replace(self._token, "[REDACTED]").strip()[:300]
                parts.append(safe)
        return f": {'; '.join(parts)}" if parts else ""

    @staticmethod
    def _object(value: object, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise BangumiApiError(f"Bangumi {label}不是对象")
        return value
