"""TMDB 匹配:标题+年份硬校验;返回海报/评分/概览等卡片素材。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.media import MediaInfo, title_match

logger = logging.getLogger(__name__)

_API = "https://api.themoviedb.org/3"
_POSTER = "https://image.tmdb.org/t/p/w500"


@dataclass
class TmdbMatch:
    tmdb_id: int
    media_type: str            # "movie" | "tv"
    title: str
    year: int | None
    poster_path: str | None    # 拼 _POSTER
    rating: float | None
    overview: str
    genres: list[str]
    runtime_min: int | None    # 电影时长
    seasons: int | None        # 剧集季数
    episodes: int | None       # 剧集总集数


class TmdbClient:
    def __init__(self, api_key: str, proxy: str = "") -> None:
        self._key = api_key
        # v4 token(ey 开头的 JWT)用 Bearer;v3 key(32位)走 api_key 参数
        self._bearer = api_key if api_key.startswith("eyJ") else ""
        headers = {"Authorization": f"Bearer {api_key}"} if self._bearer else {}
        self._client = httpx.AsyncClient(
            timeout=15, proxy=proxy or None, headers=headers,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict | None:
        if not self._bearer:
            params = {**params, "api_key": self._key}
        try:
            r = await self._client.get(f"{_API}{path}", params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.warning("TMDB 请求失败 %s %s: %s", path, params, exc)
            return None

    async def match(self, media: MediaInfo) -> TmdbMatch | None:
        """搜索并硬校验(标题 match + 年份接近);不中返回 None。"""
        if not media.title:
            return None
        year = media.year
        # 先按媒体类型搜索:剧集优先 tv,电影优先 movie,各自带年份
        order = ("tv", "movie") if media.is_tv else ("movie", "tv")
        for mtype in order:
            params = {"query": media.title, "include_adult": "false"}
            if year:
                params["first_air_date_year" if mtype == "tv" else "year"] = year
            data = await self._get(f"/search/{mtype}", params)
            results = (data or {}).get("results") or []
            for item in results[:5]:
                cand_title = item.get("title") or item.get("name") or ""
                cand_year_raw = item.get("release_date") or item.get("first_air_date") or ""
                cand_year = int(cand_year_raw[:4]) if cand_year_raw[:4].isdigit() else None
                if not title_match(media.title, cand_title):
                    continue
                if year and cand_year:
                    # 电影严格相等;剧集允许 ±1(跨年播)
                    tol = 1 if mtype == "tv" else 0
                    if abs(year - cand_year) > tol:
                        continue
                return await self._details(mtype, int(item["id"]))
        # 兜底:不带年份再试一轮(年份缺失时)
        if not year:
            for mtype in order:
                data = await self._get(f"/search/{mtype}", {"query": media.title})
                for item in ((data or {}).get("results") or [])[:3]:
                    cand_title = item.get("title") or item.get("name") or ""
                    if title_match(media.title, cand_title):
                        return await self._details(mtype, int(item["id"]))
        return None

    async def _details(self, mtype: str, tmdb_id: int) -> TmdbMatch | None:
        data = await self._get(f"/{mtype}/{tmdb_id}", {"language": "zh-CN"})
        if not data:
            return None
        date = data.get("release_date") or data.get("first_air_date") or ""
        runtime = None
        seasons_n = episodes_n = None
        if mtype == "movie":
            runtime = data.get("runtime")
        else:
            seasons_n = len(data.get("seasons") or [])
            episodes_n = sum(s.get("episode_count") or 0 for s in data.get("seasons") or [])
        return TmdbMatch(
            tmdb_id=tmdb_id,
            media_type=mtype,
            title=data.get("title") or data.get("name") or "",
            year=int(date[:4]) if date[:4].isdigit() else None,
            poster_path=data.get("poster_path"),
            rating=(data.get("vote_average") or None) and round(float(data["vote_average"]), 1),
            overview=(data.get("overview") or "").strip(),
            genres=[g["name"] for g in data.get("genres") or []],
            runtime_min=runtime,
            seasons=seasons_n,
            episodes=episodes_n,
        )

    async def fetch_poster(self, match: TmdbMatch) -> bytes | None:
        if not match.poster_path:
            return None
        try:
            r = await self._client.get(f"{_POSTER}{match.poster_path}")
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as exc:
            logger.warning("海报下载失败:%s", exc)
            return None
