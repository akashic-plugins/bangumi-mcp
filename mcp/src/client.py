from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote

import requests

from .config import BangumiRuntimeConfig


API_BASE_URL = "https://api.bgm.tv"
REQUEST_TIMEOUT = (3.05, 15.0)


class BangumiApiError(RuntimeError):
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

    def list_collections(
        self,
        username: str,
        *,
        subject_type: int | None = None,
        collection_type: int | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """读取一页用户收藏，不自动追逐后续分页。"""

        _request_int(limit, "limit", minimum=1, maximum=50)
        _request_int(offset, "offset", minimum=0)
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if subject_type is not None:
            params["subject_type"] = subject_type
        if collection_type is not None:
            params["type"] = collection_type

        encoded_username = quote(username, safe="")
        payload = self._object(
            self._request_json(
                "GET",
                f"/v0/users/{encoded_username}/collections",
                params=params,
            ),
            "收藏分页",
        )
        total = _response_int(payload.get("total"), "收藏分页 total", minimum=0)
        response_limit = _response_int(
            payload.get("limit"),
            "收藏分页 limit",
            minimum=1,
            maximum=50,
        )
        response_offset = _response_int(
            payload.get("offset"),
            "收藏分页 offset",
            minimum=0,
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise BangumiApiError("Bangumi 收藏分页 data 不是数组")
        items = [self._object(item, "收藏分页条目") for item in data]
        if response_limit != limit or response_offset != offset:
            raise BangumiApiError("Bangumi 收藏分页参数与请求不匹配")
        if len(items) > response_limit:
            raise BangumiApiError("Bangumi 收藏分页返回条目超过 limit")
        if items and response_offset + len(items) > total:
            raise BangumiApiError("Bangumi 收藏分页条目超过 total")
        return {
            "total": total,
            "limit": response_limit,
            "offset": response_offset,
            "data": items,
        }

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

    def list_episodes(
        self,
        subject_id: int,
        *,
        episode_type: int = 0,
    ) -> list[dict[str, Any]]:
        """分页读取条目的章节目录。"""

        offset = 0
        limit = 100
        result: list[dict[str, Any]] = []
        while True:
            payload = self._object(
                self._request_json(
                    "GET",
                    "/v0/episodes",
                    params={
                        "subject_id": subject_id,
                        "type": episode_type,
                        "offset": offset,
                        "limit": limit,
                    },
                ),
                "章节目录分页",
            )
            total = _response_int(
                payload.get("total"),
                "章节目录分页 total",
                minimum=0,
            )
            response_limit = _response_int(
                payload.get("limit"),
                "章节目录分页 limit",
                minimum=1,
                maximum=100,
            )
            response_offset = _response_int(
                payload.get("offset"),
                "章节目录分页 offset",
                minimum=0,
            )
            data = payload.get("data")
            if not isinstance(data, list):
                raise BangumiApiError("Bangumi 章节目录分页 data 不是数组")
            page = [self._object(item, "章节目录条目") for item in data]
            if response_limit != limit or response_offset != offset:
                raise BangumiApiError("Bangumi 章节目录分页参数与请求不匹配")
            if len(page) > response_limit:
                raise BangumiApiError("Bangumi 章节目录分页返回条目超过 limit")
            if page and response_offset + len(page) > total:
                raise BangumiApiError("Bangumi 章节目录分页条目超过 total")
            result.extend(page)
            offset += len(page)
            if offset >= total:
                return result
            if not page:
                raise BangumiApiError("Bangumi 章节目录分页提前结束")

    def get_episode_collection(self, episode_id: int) -> dict[str, Any]:
        """读取当前 Token 用户对单集的精确收藏状态。"""

        return self._object(
            self._request_json(
                "GET",
                f"/v0/users/-/collections/-/episodes/{episode_id}",
            ),
            "单集章节收藏",
        )

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
                retry_after_seconds=_retry_after_seconds(response),
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


def _request_int(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = (
            f"{minimum} 至 {maximum}"
            if maximum is not None
            else f"至少 {minimum}"
        )
        raise ValueError(f"{label} 必须为 {bounds}")
    return value


def _response_int(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        return _request_int(
            value,
            label,
            minimum=minimum,
            maximum=maximum,
        )
    except ValueError as error:
        raise BangumiApiError(str(error)) from None


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
