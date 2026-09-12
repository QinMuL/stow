"""TMDB 匹配单测:年份硬门槛 / 唯一选择 / 两轮搜索 / 标注直连 / 别名兜底。"""

from __future__ import annotations

import pytest

from app.media import AggregatedMedia
from app.tmdb import (
    TmdbClient,
    _filter_candidates,
    _normalize,
    _pick_best,
    image_url,
)


def _media(**kw):
    base = dict(title="Show", year=2024, media_type="tv")
    base.update(kw)
    return AggregatedMedia(**base)


def _cand(cid, title, year, *, kind="tv", original=""):
    date_key = "release_date" if kind == "movie" else "first_air_date"
    title_key = "title" if kind == "movie" else "name"
    orig_key = "original_title" if kind == "movie" else "original_name"
    return {
        "id": cid, title_key: title, orig_key: original or title,
        date_key: f"{year}-01-01",
    }


# ── 年份硬门槛 ──────────────────────────────────────────────
def test_filter_movie_year_strict():
    media = _media(media_type="movie", year=2018)
    m, _ = _filter_candidates(
        [_cand(1, "A Quiet Place", 2018, kind="movie"),
         _cand(2, "A Quiet Place", 2020, kind="movie")],
        media, ["A Quiet Place"], "movie",
    )
    assert [c["id"] for c in m] == [1]


def test_filter_tv_first_air_not_later():
    # 剧集:首播年不得晚于资源年(资源 2026,首播 2023 的在播剧应命中)
    media = _media(year=2026)
    m, _ = _filter_candidates(
        [_cand(1, "Lioness", 2023), _cand(2, "Lioness", 2027)],
        media, ["Lioness"], "tv",
    )
    assert [c["id"] for c in m] == [1]


def test_filter_near_miss_kept_for_alias_fallback():
    # 类型+年份对但标题不中 → 进 near(别名兜底候选),不直接丢弃
    media = _media(year=2024)
    m, near = _filter_candidates(
        [_cand(1, "完全不同的标题", 2024)], media, ["Show"], "tv",
    )
    assert m == [] and len(near) == 1


# ── 唯一选择 ────────────────────────────────────────────────
def test_pick_best_single():
    assert _pick_best([_cand(1, "X", 2020)], _media(), "tv")["id"] == 1


def test_pick_best_ambiguous_without_year():
    assert _pick_best([_cand(1, "X", 2020), _cand(2, "X", 2021)], _media(year=None), "tv") is None


def test_pick_best_tv_latest_first_air():
    # 同名剧集多命中取首播最晚
    best = _pick_best([_cand(1, "X", 2010), _cand(2, "X", 2020)], _media(year=2024), "tv")
    assert best["id"] == 2


# ── 图片 URL ────────────────────────────────────────────────
def test_image_url_prefers_backdrop():
    assert image_url({"backdrop_path": "/b.jpg", "poster_path": "/p.jpg"}).endswith("/b.jpg")
    assert image_url({"backdrop_path": None, "poster_path": "/p.jpg"}).endswith("/p.jpg")
    assert image_url({}) is None


@pytest.mark.asyncio
async def test_fetch_image_retries_transient_error():
    """海报下载遇瞬时错误应重试(GET 幂等),两次都失败才放弃。"""
    import httpx

    client = TmdbClient("k" * 32)
    calls = {"n": 0}

    class _Resp:
        content = b"img-bytes"

        def raise_for_status(self) -> None:
            pass

    class _Stub:
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("")
            return _Resp()

    client._client = _Stub()
    assert await client.fetch_image("https://x/y.jpg") == b"img-bytes"
    assert calls["n"] == 2

    class _Dead:
        async def get(self, url):
            calls["n"] += 1
            raise httpx.ConnectError("")

    client._client = _Dead()
    assert await client.fetch_image("https://x/y.jpg") is None
    assert calls["n"] == 4  # 又试了两次


# ── 详情归一化 ──────────────────────────────────────────────
def _raw_tv():
    return {
        "id": 1311031, "name": "藏锋", "original_name": "Sharp Turns",
        "first_air_date": "2025-03-01", "overview": " 简介 ",
        "poster_path": "/p.jpg", "backdrop_path": None,
        "vote_average": 7.8, "vote_count": 55,
        "genres": [{"name": "剧情"}, {"name": "武侠"}], "status": "Returning Series",
        "created_by": [{"name": "某导演"}],
        "number_of_seasons": 1, "number_of_episodes": 12,
        "seasons": [{"season_number": 1, "episode_count": 12, "name": "第 1 季"}],
        "origin_country": ["CN"],
        "credits": {"cast": [{"name": "甲"}, {"name": "乙"}]},
        "translations": {"translations": [
            {"iso_639_1": "en", "data": {"name": "Sharp Turns"}},
        ]},
        "alternative_titles": {"results": [{"title": "另名"}]},
    }


def test_normalize_tv():
    d = _normalize(_raw_tv(), "tv")
    assert d["tmdb_id"] == 1311031 and d["media_type"] == "tv"
    assert d["title"] == "藏锋" and d["original_title"] == "Sharp Turns"
    assert d["year"] == 2025 and d["overview"] == "简介"
    assert d["genres"] == ["剧情", "武侠"] and d["cast"] == ["甲", "乙"]
    assert d["seasons"][0]["episode_count"] == 12
    assert "Sharp Turns" in d["alt_titles"] and "另名" in d["alt_titles"]


def test_normalize_movie_directors():
    raw = {
        "id": 1, "title": "寂静之地", "original_title": "A Quiet Place",
        "release_date": "2018-05-18", "runtime": 90,
        "genres": [], "credits": {"cast": [], "crew": [
            {"name": "导演A", "job": "Director"}, {"name": "编剧", "job": "Screenplay"},
        ]},
    }
    d = _normalize(raw, "movie")
    assert d["runtime"] == 90 and d["directors"] == ["导演A"]
    assert d["year"] == 2018


# ── match 流程(monkeypatch 网络层) ─────────────────────────
@pytest.mark.asyncio
async def test_match_two_round_zh_then_en(monkeypatch):
    """zh-CN 无结果 → 英文查询词走 en-US 命中。"""
    client = TmdbClient("k" * 32)

    async def fake_get(self, path, params, language=None):
        if path == "/search/tv" and language == "en-US":
            return {"results": [_cand(7, "Sharp Turns", 2025)]}
        if path == "/tv/7":
            return _raw_tv() | {"id": 7}
        return {"results": []}  # zh-CN 搜索:空

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    d = await client.match(_media(title="Sharp Turns", year=2025))
    assert d and d["tmdb_id"] == 7


@pytest.mark.asyncio
async def test_match_tmdb_id_annotation(monkeypatch):
    """{tmdb-ID} 标注直连详情;文件名英文但 alt_titles 命中。"""
    client = TmdbClient("k" * 32)
    calls: list[str] = []

    async def fake_get(self, path, params, language=None):
        calls.append(path)
        assert path == "/tv/1311031"
        return _raw_tv()

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    d = await client.match(_media(title="Sharp Turns", tmdb_id=1311031))
    assert d and d["title"] == "藏锋"
    assert calls == ["/tv/1311031"]  # 只查详情,无搜索


@pytest.mark.asyncio
async def test_match_prefers_exact_title_over_prefix_weak_match(monkeypatch):
    """2026-09-12 真实误配回归:`Made in Korea` 资源被前缀弱命中抢走。

    线上现象:`Made.in.Korea.S02E01.2026...` 匹配成了真人秀《韩国制造:k-pop体验》,
    应该是正剧《韩国制造》(TMDB 246473)。两处规则缺陷:
    ① 查询词 "Made in Korea" 是真人秀原名 "Made in Korea: The K-Pop Experience" 的**前缀**
       → 弱命中直接进 matched;
    ② 别名兜底原来只在"零命中"时才跑(`if not matched and near`),正剧中文名/韩文原名都不含
       查询词、只能靠 translations 命中,因此落在 near 里,被短路彻底跳过 → 单候选直取。
    修复后:别名兜底始终跑 → 正剧进池;"标题完全相等"者优先 → 选中正剧。
    """
    client = TmdbClient("k" * 32)

    reality = {"id": 260681, "name": "韩国制造：k-pop体验",
               "original_name": "Made in Korea: The K-Pop Experience",
               "first_air_date": "2024-08-17"}
    drama = {"id": 246473, "name": "韩国制造", "original_name": "메이드 인 코리아",
             "first_air_date": "2025-12-25"}

    def raw(cid, name, original, seasons):
        return {
            "id": cid, "name": name, "original_name": original,
            "first_air_date": "2024-01-01", "number_of_seasons": len(seasons),
            "seasons": [{"season_number": s, "episode_count": 6} for s in seasons],
            "translations": {"translations": []}, "alternative_titles": {"results": []},
        }

    async def fake_get(self, path, params, language=None):
        if path == "/search/tv":
            return {"results": [reality, drama]}
        if path == "/tv/260681":
            return raw(260681, "韩国制造：k-pop体验", "Made in Korea: The K-Pop Experience", [1, 2])
        if path == "/tv/246473":
            d = raw(246473, "韩国制造", "메이드 인 코리아", [1, 2])
            d["translations"] = {"translations": [
                {"iso_639_1": "en", "data": {"name": "Made in Korea"}}]}   # ← 正剧的英文别名
            return d
        return {}

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    d = await client.match(_media(title="Made in Korea", year=2026, season=2))
    assert d and d["tmdb_id"] == 246473, "应选中正剧《韩国制造》,而不是前缀命中的真人秀"


@pytest.mark.asyncio
async def test_match_keeps_single_weak_match(monkeypatch):
    """只有一条弱命中(前缀)时仍要接受:不能为了防误配把能匹配的也拦掉。"""
    client = TmdbClient("k" * 32)

    async def fake_get(self, path, params, language=None):
        if path == "/search/tv":
            return {"results": [{"id": 260681, "name": "韩国制造：k-pop体验",
                                 "original_name": "Made in Korea: The K-Pop Experience",
                                 "first_air_date": "2024-08-17"}]}
        if path == "/tv/260681":
            return {"id": 260681, "name": "韩国制造：k-pop体验",
                    "original_name": "Made in Korea: The K-Pop Experience",
                    "first_air_date": "2024-08-17", "seasons": [],
                    "translations": {"translations": []}, "alternative_titles": {"results": []}}
        return {}

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    d = await client.match(_media(title="Made in Korea", year=2026))
    assert d and d["tmdb_id"] == 260681


def test_pick_best_single_candidate_year_gap_only_warns(caplog):
    """唯一候选年份差得离谱 → 只打提示日志,不拦截匹配(回归:不能硬拒)。"""
    import logging

    media = _media(year=2026)
    old = _cand(1, "老剧", 2010)
    with caplog.at_level(logging.WARNING, logger="app.tmdb"):
        picked = _pick_best([old], media, "tv")
    assert picked is old, "提示不等于拦截:能匹配的仍要匹配(如《完美世界》2021→2026 资源)"
    assert any("相差 16 年" in r.getMessage() for r in caplog.records)


def test_pick_best_single_candidate_close_year_silent(caplog):
    """年份接近则不吵(5 年内的在播剧很常见)。"""
    import logging

    media = _media(year=2026)
    with caplog.at_level(logging.WARNING, logger="app.tmdb"):
        _pick_best([_cand(1, "在播剧", 2021)], media, "tv")
    assert not [r for r in caplog.records if "年份相差" in r.getMessage()]


@pytest.mark.asyncio
async def test_failure_log_never_leaks_api_key(monkeypatch, caplog):
    """请求失败日志必须脱敏:api_key 不能写进日志文件(2026-09-12 发现)。"""
    import logging

    import httpx

    from app.tmdb import _mask

    assert _mask({"query": "x", "api_key": "secret123", "Authorization": "Bearer t"}) == {
        "query": "x", "api_key": "***", "Authorization": "***"}

    class _Boom:
        async def get(self, url, params=None):
            raise httpx.ConnectError("boom")

    client = TmdbClient("k" * 32)
    client._client = _Boom()
    with caplog.at_level(logging.WARNING, logger="app.tmdb"):
        await client._get("/search/tv", {"query": "x"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "kkkkkkkk" not in text and "***" in text and "boom" in text


@pytest.mark.asyncio
async def test_match_annotation_mismatch_falls_back(monkeypatch):
    """标注与标题对不上 → 忽略标注走常规搜索。"""
    client = TmdbClient("k" * 32)

    async def fake_get(self, path, params, language=None):
        if path == "/tv/999":
            # 与搜索词毫无交集的详情(无英文别名)
            return _raw_tv() | {
                "id": 999, "name": "毫不相干", "original_name": "Nope",
                "translations": {}, "alternative_titles": {},
            }
        if path == "/search/tv":
            return {"results": [_cand(1311031, "藏锋", 2025)]}
        if path == "/tv/1311031":
            return _raw_tv()
        return {}

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    d = await client.match(_media(title="Sharp Turns", tmdb_id=999, year=2025))
    assert d and d["tmdb_id"] == 1311031


@pytest.mark.asyncio
async def test_match_near_miss_alias_fallback(monkeypatch):
    """zh-CN 结果标题是中文,搜索词英文:拉详情别名后命中。"""
    client = TmdbClient("k" * 32)

    async def fake_get(self, path, params, language=None):
        if path == "/search/tv":
            return {"results": [_cand(1311031, "藏锋", 2025)]}
        if path == "/tv/1311031":
            return _raw_tv()  # alt_titles 含 "Sharp Turns"
        return {}

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    d = await client.match(_media(title="Sharp Turns", year=2025))
    assert d and d["tmdb_id"] == 1311031


@pytest.mark.asyncio
async def test_match_no_result_returns_none(monkeypatch):
    client = TmdbClient("k" * 32)

    async def fake_get(self, path, params, language=None):
        return {"results": []}

    monkeypatch.setattr(TmdbClient, "_get", fake_get)
    assert await client.match(_media(title="不存在的片名")) is None


# ── 剧集多候选消歧(实测案例:茶啊二中 vs 茶啊二中叭叭叭) ──────
def _client_with_seasons(seasons_by_id: dict[int, list[int]]):
    """构造不发请求的客户端:get_details 用假数据替换。"""
    client = TmdbClient("dummy-key")

    async def fake_details(tid, media_type):
        if tid not in seasons_by_id:
            return None
        return {"title": f"t{tid}", "seasons": [{"season": s} for s in seasons_by_id[tid]]}

    client.get_details = fake_details  # type: ignore[method-assign]
    return client


def test_tv_disambiguation_prefers_season_coverage():
    """资源 S06E01:只有 6 季的《茶啊二中》覆盖第 6 季 → 选它(而非首播最晚的衍生剧)。"""
    import asyncio

    client = _client_with_seasons({
        119059: [0, 1, 2, 3, 4, 5, 6],     # 正主(2014,6 季)
        195224: [1],                        # 茶啊二中叭叭叭(2017,1 季)
        237306: [1],                        # 泡面番(2016,1 季)
    })
    media = _media(title="茶啊二中", year=2025, season=6, episode_start=1)
    matched = [_cand(119059, "茶啊二中", 2014), _cand(195224, "茶啊二中叭叭叭", 2017),
               _cand(237306, "茶啊二中泡面番", 2016)]
    picked = asyncio.run(client._pick_by_season(matched, media))
    assert picked is not None and picked["id"] == 119059
    asyncio.run(client.aclose())


def test_tv_disambiguation_probes_beyond_newest_when_needed():
    """覆盖该季的候选可能排在后面:排序要优先"标题完全相等"的(否则探不到)。"""
    import asyncio

    client = _client_with_seasons({1: [1], 2: [1], 3: [1], 4: [1], 5: [1],
                                   9: [0, 1, 2, 3, 4, 5, 6]})
    media = _media(title="正主剧", year=2025, season=6, episode_start=1)
    matched = [_cand(i, f"正主剧衍生{i}", 2015 + i) for i in range(1, 6)]
    matched.append(_cand(9, "正主剧", 2011))          # 标题全等但年份最老
    picked = asyncio.run(client._pick_by_season(matched, media))
    assert picked is not None and picked["id"] == 9
    asyncio.run(client.aclose())


def test_tv_disambiguation_falls_back_when_none_covers():
    """候选都不覆盖该季 → 返回 None,交回原规则(取首播最晚),不阻断匹配。"""
    import asyncio

    client = _client_with_seasons({1: [1], 2: [1, 2]})
    media = _media(title="某剧", year=2025, season=9, episode_start=1)
    matched = [_cand(1, "某剧", 2014), _cand(2, "某剧第二季", 2016)]
    assert asyncio.run(client._pick_by_season(matched, media)) is None
    asyncio.run(client.aclose())
