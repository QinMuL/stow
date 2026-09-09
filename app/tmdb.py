"""TMDB 匹配:{tmdb-ID} 标注直连 + 两轮搜索(中→英)+ 别名兜底 + 年份硬门槛。

匹配不到返回 None(卡片降级纯文件名信息);详情归一化为统一 dict 供卡片渲染。
"""

from __future__ import annotations

import logging

import httpx

from app.media import AggregatedMedia, title_match

logger = logging.getLogger(__name__)

_API = "https://api.themoviedb.org/3"
_POSTER = "https://image.tmdb.org/t/p/w500"
_BACKDROP = "https://image.tmdb.org/t/p/w780"

# 别名兜底最多查几条详情(防候选多打爆 TMDB 限流)
_NEAR_MISS_LIMIT = 5


def poster_url(path: str | None) -> str | None:
    return f"{_POSTER}{path}" if path else None


def backdrop_url(path: str | None) -> str | None:
    return f"{_BACKDROP}{path}" if path else None


def image_url(details: dict) -> str | None:
    """卡片配图:优先横屏 backdrop,回退竖屏 poster。"""
    return backdrop_url(details.get("backdrop_path")) or poster_url(details.get("poster_path"))


def _title_pool(details: dict) -> list[str]:
    """详情级标题池:本地名 + 原名 + 别名。"""
    pool = [details.get("title") or "", details.get("original_title") or ""]
    pool.extend(details.get("alt_titles") or [])
    return [t for t in pool if t]


def _cand_year(c: dict) -> int | None:
    d = c.get("release_date") or c.get("first_air_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


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

    async def _get(self, path: str, params: dict, language: str | None = None) -> dict | None:
        if language:
            params = {**params, "language": language}
        if not self._bearer:
            params = {**params, "api_key": self._key}
        try:
            r = await self._client.get(f"{_API}{path}", params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.warning("TMDB 请求失败 %s %s: %s", path, params, exc)
            return None

    # ── 搜索 ───────────────────────────────────────────────
    async def _search_one(self, query: str, year: int | None, mtype: str, language: str) -> list[dict]:
        """单类型搜索;带年无果回退无年。"""
        params: dict = {"query": query, "include_adult": "false"}
        if year:
            params["year" if mtype == "movie" else "first_air_date_year"] = year
        data = await self._get(f"/search/{mtype}", params, language)
        results = (data or {}).get("results") or []
        if not results and year:
            data = await self._get(f"/search/{mtype}", {"query": query, "include_adult": "false"}, language)
            results = (data or {}).get("results") or []
        return results

    # ── 匹配主流程 ─────────────────────────────────────────
    async def match(self, media: AggregatedMedia) -> dict | None:
        """返回归一化详情 dict;无把握匹配返回 None。"""
        # 1. {tmdb-XXX} 标注:分享者/媒体工具标注,最可靠;标题对不上则忽略走常规搜索
        if media.tmdb_id:
            details = await self.get_details(media.tmdb_id, media.media_type)
            if details and (not media.title or title_match(media.title, _title_pool(details))):
                return details
            logger.info("TMDB 标注 %s 与标题不符,回退常规搜索", media.tmdb_id)
        if not media.title:
            return None

        # 2. 两轮搜索:zh-CN → 英文查询词走 en-US;主类型无果再试另一类型
        queries = [media.title, *media.alt_queries]
        for mtype in (media.media_type, "tv" if media.media_type == "movie" else "movie"):
            best = await self._search_round(queries, media, mtype, "zh-CN")
            if best is None:
                ascii_queries = [q for q in queries if q.isascii() and q.strip()]
                if ascii_queries:
                    best = await self._search_round(ascii_queries, media, mtype, "en-US")
            if best is not None:
                return await self.get_details(int(best["id"]), mtype)
        return None

    async def _search_round(
        self, queries: list[str], media: AggregatedMedia, mtype: str, language: str
    ) -> dict | None:
        """一轮搜索:聚合候选 → 硬门槛过滤 → 别名兜底 → 唯一选择。"""
        candidates: list[dict] = []
        seen: set[int] = set()
        for q in queries:
            for c in await self._search_one(q, media.year, mtype, language):
                cid = int(c.get("id") or 0)
                if cid and cid not in seen:
                    seen.add(cid)
                    candidates.append(c)
        if not candidates:
            return None

        matched, near = _filter_candidates(candidates, media, queries, mtype)
        if not matched and near:
            # 类型+年份全对但标题不中(zh-CN 条目只有中文标题的典型场景):
            # 拉详情的 translations/AKA 别名再判一次
            for c in near[:_NEAR_MISS_LIMIT]:
                d = await self.get_details(int(c["id"]), mtype)
                if d and any(title_match(q, _title_pool(d)) for q in queries):
                    matched.append(c)
        if not matched:
            return None
        return _pick_best(matched, media, mtype)

    # ── 详情(归一化) ─────────────────────────────────────
    async def get_details(self, tmdb_id: int, media_type: str) -> dict | None:
        params = {"append_to_response": "credits,translations,alternative_titles"}
        data = await self._get(f"/{media_type}/{tmdb_id}", params, "zh-CN")
        if not data:
            return None
        return _normalize(data, media_type)

    async def fetch_image(self, url: str, attempts: int = 2) -> bytes | None:
        """下载图片(走配置代理,幂等 GET 失败重试一次)。

        send_photo 用 URL 会让 Telegram 服务器拉图,实测 TMDB 图源常超时;
        改为本地经代理取字节再发,链路可控。"""
        for attempt in range(1, attempts + 1):
            try:
                r = await self._client.get(url)
                r.raise_for_status()
                return r.content
            except httpx.HTTPError as exc:  # httpx 异常 str 常为空,带上类型名
                logger.warning(
                    "图片下载失败(%s: %s)第 %d/%d 次", type(exc).__name__, exc, attempt, attempts
                )
        return None


def _filter_candidates(
    candidates: list[dict], media: AggregatedMedia, queries: list[str], mtype: str
) -> tuple[list[dict], list[dict]]:
    """硬门槛过滤:年份 + 标题。

    年份门槛:电影严格相等;剧集首播年不得晚于资源年(在播剧跨年)。
    返回 (matched, near):标题命中的 / 年份类型对但标题没中的(别名兜底候选)。
    """
    matched: list[dict] = []
    near: list[dict] = []
    for c in candidates:
        cy = _cand_year(c)
        if media.year and cy:
            if mtype == "movie" and cy != media.year:
                continue
            if mtype == "tv" and cy > media.year:
                continue
        titles = [
            c.get("title") or c.get("name") or "",
            c.get("original_title") or c.get("original_name") or "",
        ]
        if any(title_match(q, titles) for q in queries):
            matched.append(c)
        else:
            near.append(c)
    return matched, near


def _pick_best(matched: list[dict], media: AggregatedMedia, mtype: str) -> dict | None:
    """唯一选择:单条直取;无年份多候选无法消歧放弃;剧集取首播最晚。"""
    if len(matched) == 1:
        return matched[0]
    if media.year is None:
        return None
    if mtype == "tv":
        return max(matched, key=lambda c: _cand_year(c) or 0)
    return matched[0]


def _collect_alt_titles(data: dict) -> list[str]:
    """从 translations + alternative_titles 收集别名(en/zh/ko + 无地区 AKA,去重保序)。"""
    titles: list[str] = []
    for t in data.get("translations", {}).get("translations", []) or []:
        lang = t.get("iso_639_1", "")
        name = (t.get("data") or {}).get("name") or (t.get("data") or {}).get("title") or ""
        if lang in ("en", "zh", "ko") and name:
            titles.append(name.strip())
    at = data.get("alternative_titles") or {}
    for item in at.get("results", []) or at.get("titles", []) or []:
        name = item.get("title") if isinstance(item, dict) else str(item or "")
        if name:
            titles.append(name.strip())
    seen: set[str] = set()
    uniq: list[str] = []
    for n in titles:
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def _year_of(date_str: str | None) -> int | None:
    return int(date_str[:4]) if date_str and date_str[:4].isdigit() else None


def _normalize(data: dict, media_type: str) -> dict:
    """TMDB 原始详情 → 卡片/匹配用的统一字段。"""
    alt_titles = _collect_alt_titles(data)
    countries = data.get("origin_country") or [
        c.get("iso_3166_1") for c in data.get("production_countries", [])
    ]
    credits = data.get("credits", {})
    common = {
        "tmdb_id": data.get("id"),
        "media_type": media_type,
        "title": data.get("title") or data.get("name") or "",
        "original_title": data.get("original_title") or data.get("original_name") or "",
        "alt_titles": alt_titles,
        "year": _year_of(data.get("release_date") or data.get("first_air_date")),
        "release_date": data.get("release_date") or data.get("first_air_date") or "",
        "overview": (data.get("overview") or "").strip(),
        "poster_path": data.get("poster_path"),
        "backdrop_path": data.get("backdrop_path"),
        "vote_average": data.get("vote_average") or 0,
        "vote_count": data.get("vote_count") or 0,
        "genres": [g["name"] for g in data.get("genres", [])],
        "status": data.get("status") or "",
        "cast": [c["name"] for c in credits.get("cast", [])][:5],
        "countries": countries,
    }
    if media_type == "movie":
        return {
            **common,
            "runtime": data.get("runtime"),
            "directors": [
                c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"
            ][:3],
        }
    seasons = [
        {
            "season": s.get("season_number"),
            "episode_count": s.get("episode_count") or 0,
            "name": s.get("name") or "",
        }
        for s in data.get("seasons", [])  # 保留 season 0(特别篇),S00 文件能正确匹配
    ]
    return {
        **common,
        "number_of_seasons": data.get("number_of_seasons") or 0,
        "number_of_episodes": data.get("number_of_episodes") or 0,
        "creators": [c["name"] for c in data.get("created_by", [])][:3],
        "seasons": seasons,
    }
