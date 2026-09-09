"""阶段一/二单元测试:链接解析 / 文件名解析 / 聚合 / 卡片 / 去重。"""

from __future__ import annotations

import pytest

import app.pan115 as pan
from app import card
from app.links import ParsedLink, parse_all, parse_one
from app.media import (
    analyze_share,
    clean_name,
    extract_platform,
    extract_season_episode,
    extract_tmdb_id,
    get_quality_info,
    parse_filename,
    title_match,
)
from app.pan115 import ShareFile, fmt_size
from app.store import Store


# ── 链接解析(115 + ed2k) ───────────────────────────────────
def test_parse_url_with_password():
    links = parse_all("看这个 https://115.com/s/abc123xyz?password=ef12")
    assert len(links) == 1 and links[0].provider == "115"
    assert links[0].code == "abc123xyz" and links[0].password == "ef12"


def test_parse_url_tail_token():
    links = parse_all("https://115.com/s/abc123xyz?ef12ef12")
    assert links[0].password == "ef12ef12"


def test_parse_body_access_code():
    links = parse_all("https://115.com/s/abc123xyz 访问码：ef12")
    assert links[0].password == "ef12"


def test_parse_multiple_dedup():
    text = "https://115.com/s/aaa11122?a=1 https://115.com/s/bbb22233 https://115.com/s/aaa11122"
    assert [lk.code for lk in parse_all(text)] == ["aaa11122", "bbb22233"]


def test_parse_single_bare_code():
    link = parse_one("abc12345xyz")
    assert link is not None and link.provider == "115" and link.code == "abc12345xyz"


def test_parse_single_ignores_short_words():
    assert parse_one("hello") is None


_ED2K = "ed2k://|file|The.Movie.2023.1080p.mkv|2345678901|0123456789ABCDEF0123456789ABCDEF|h=XYZ|/"


def test_parse_ed2k_link():
    links = parse_all(f"推这个 {_ED2K}")
    assert len(links) == 1 and links[0].provider == "ed2k"
    assert links[0].key == "0123456789ABCDEF0123456789ABCDEF"  # 去重 key 用文件 hash
    from app.links import ed2k_file
    name, size, fh = ed2k_file(links[0].code)
    assert name == "The.Movie.2023.1080p.mkv" and size == 2345678901


def test_parse_mixed_115_and_ed2k():
    text = f"{_ED2K} 和 https://115.com/s/aaa11122"
    links = parse_all(text)
    assert [p.provider for p in links] == ["ed2k", "115"]  # 按出现顺序


def test_parse_ed2k_dedup_by_hash():
    # 同文件 hash 不同尾参 → 去重
    links = parse_all(f"{_ED2K} {_ED2K[:-2]}p=/")
    assert len(links) == 1


def test_card_ed2k_footer():
    from app.media import analyze_share

    m = analyze_share(_files("The.Movie.2023.1080p.mkv"))
    text = card.render_caption(m, None, ParsedLink("ed2k", _ED2K, _ED2K, file_hash="0" * 32))
    assert "ed2k 资源" in text and "<code>" in text


def test_channel_routing_preset():
    from app.config import ChannelConfig, Config

    cfg = Config()
    assert cfg.channel_for("115") is None  # 未登记归属:None(不推送,提示登记)
    cfg.channels = [ChannelConfig(chat_id="-100A", preset="ed2k")]
    assert cfg.channel_for("ed2k") == "-100A"
    assert cfg.channel_for("115") is None


@pytest.mark.asyncio
async def test_snap_transport_error_downgrades(monkeypatch):
    """share_snap HTTP 层失败(405 风控等)须降级返回 None,由枚举兜底,不让链路卡死。"""
    monkeypatch.setattr(pan, "_HTTP_RETRY_WAIT", 0.0)
    r = pan.Pan115Reader()

    async def boom(*args, **kwargs):
        raise RuntimeError("HTTP Error 405: Method Not Allowed")

    monkeypatch.setattr(r, "_call", boom)
    assert await r._snap("abc12345xyz", "") is None


@pytest.mark.asyncio
async def test_read_share_walk_onerror_and_dirs(monkeypatch):
    """walk 必须 onerror=True(默认 False 会静默跳过列表失败的目录 → 漏季);子目录须收集。"""
    import p115client.tool as ptool

    kwargs: dict = {}

    def fake_walk(client, code, pwd, **kw):
        kwargs.update(kw)
        yield (0, ["Season 1"], [{"n": "a.S01E01.mkv", "s": 1024}])

    monkeypatch.setattr(ptool, "share_iterdir_walk", fake_walk)
    r = pan.Pan115Reader()

    async def snap_none(code, pwd):
        return None  # 模拟 share_snap 不可用,降级直接枚举

    monkeypatch.setattr(r, "_snap", snap_none)
    files = await r.read_share(pan.ShareLink("abc12345xyz"))
    assert kwargs.get("onerror") is True
    assert kwargs.get("cooldown") == 1.0
    assert kwargs.get("app") == "web"  # 无 cookie 走匿名 web 通道
    assert pan.ShareFile("Season 1", 0, True) in files  # 目录被收集
    assert pan.ShareFile("a.S01E01.mkv", 1024, False) in files


@pytest.mark.asyncio
async def test_read_share_cookie_uses_app_channel(monkeypatch):
    """有 cookie 走 android/proapi 通道(绕开 115 对匿名 webapi share_snap 的指纹封锁)。"""
    import p115client.tool as ptool

    kwargs: dict = {}

    def fake_walk(client, code, pwd, **kw):
        kwargs.update(kw)
        yield (0, [], [{"n": "Movie.2025.mkv", "s": 1}])

    monkeypatch.setattr(ptool, "share_iterdir_walk", fake_walk)
    r = pan.Pan115Reader("UID=123456; CID=abc; SEID=def")
    assert r.logged_in

    async def snap_none(code, pwd):
        return None

    monkeypatch.setattr(r, "_snap", snap_none)
    files = await r.read_share(pan.ShareLink("abc12345xyz"))
    assert kwargs.get("app") == "android"
    assert len(files) == 1


# ── 文件名解析(guessit 引擎) ──────────────────────────────
def test_parse_movie():
    p = parse_filename("The.Dark.Knight.2008.1080p.BluRay.x264-GRP.mkv")
    assert p.title == "The Dark Knight"
    assert p.year == 2008
    assert p.media_type == "movie"
    assert p.quality == "1080P"
    assert p.release_group == "GRP"


def test_parse_episode():
    p = parse_filename("连载动画.S01E05.2026.2160p.WEB-DL.mkv")
    assert p.media_type == "tv"
    assert p.season == 1 and p.episode == 5
    assert p.year == 2026
    assert p.quality == "4K / 2160P"


def test_parse_episode_range():
    p = parse_filename("Show.S02E01-E12.1080p.mkv")
    assert p.season == 2 and p.episode == 1
    assert p.episode_end == 12


def test_parse_chinese_title():
    p = parse_filename("狂飙.2023.4K.HDR.mkv")
    assert p.title == "狂飙"
    assert p.year == 2023


def test_clean_name_strips_noise():
    cleaned = clean_name("Movie.2023.1080p.10bit.AAC5.1.HQ.mkv")
    assert cleaned.startswith("Movie 2023 1080p")
    assert "AAC" not in cleaned and "10bit" not in cleaned and "HQ" not in cleaned


def test_extract_season_episode_forms():
    assert extract_season_episode("S01E01-E12") == (1, 1, 12)
    assert extract_season_episode("S02E05") == (2, 5, 1)
    assert extract_season_episode("Season.3") == (3, None, 0)
    assert extract_season_episode("Nothing.Here") == (None, None, 0)


def test_quality_info_8_dims():
    info = get_quality_info("Show.2024.S01E01.2160p.NF.WEB-DL.DV.P8.H.265.10bit.23.976fps.DDP5.1")
    assert info == ["4K", "Netflix", "WEB-DL", "DoVi P8", "H.265", "10-bit", "23.976fps", "DDP 5.1"]


def test_platform_expand():
    assert extract_platform("Show.1080p.DSNP.WEB-DL") == "Disney+"
    assert extract_platform("Movie.2023.1080p.BluRay") == ""


def test_tmdb_id_annotation():
    assert extract_tmdb_id(["藏锋 {tmdb-1311031}", "S01E01.mkv"]) == 1311031
    assert extract_tmdb_id(["普通目录"]) is None


def test_release_group_noise_cleaned():
    p = parse_filename("Show.2024.S01E01.1080p.4Audios.HDVWEB.mkv")
    assert p.release_group == "HDVWEB"


def test_parse_quick_skips_guessit():
    p = parse_filename("Show.2024.S02E05.1080p.WEB-DL.mkv", quick=True)
    assert p.title == "" and p.year is None and p.release_group == ""
    assert p.season == 2 and p.episode == 5  # 正则提取的字段不受影响
    assert p.quality == "1080P"


# ── 分享聚合 ────────────────────────────────────────────────
def _files(*names, size=1024, is_dir=False):
    return [ShareFile(n, size, is_dir) for n in names]


def test_analyze_share_tv():
    m = analyze_share(_files(
        "Show.2024.S01E01.1080p.mkv", "Show.2024.S01E02.1080p.mkv", "Show.2024.S01E03.1080p.mkv",
    ))
    assert m.media_type == "tv"
    assert m.title == "Show" and m.year == 2024
    assert m.seasons == [1]
    assert m.season_episodes == {1: [1, 2, 3]}
    assert m.episode_start == 1 and m.episode_end == 3
    assert m.total_episodes == 3
    assert m.quality == "1080P"


def test_analyze_share_movie_single():
    m = analyze_share(_files("Movie.Name.2025.2160p.mkv"))
    assert m.media_type == "movie"
    assert m.title == "Movie Name"


def test_analyze_share_drops_non_video():
    m = analyze_share(_files("Movie.2025.mkv", "poster.jpg", "info.nfo"))
    assert m.file_count == 1  # 只数视频
    assert m.total_size == 1024 * 3


def test_analyze_share_tmdb_id_from_dir():
    m = analyze_share(_files("藏锋 {tmdb-1311031}", is_dir=True) + _files("Sharp.Turns.S01E01.mkv"))
    assert m.tmdb_id == 1311031


def test_analyze_share_none_when_empty():
    assert analyze_share([]) is None


def test_analyze_share_sampling_large():
    # 大分享只对前 8 个文件跑 guessit(标题/年份),季集/画质来自全体正则解析
    names = [f"航海王.1999.S01E{i:04d}.1080p.WEB-DL.mkv" for i in range(1, 301)]
    m = analyze_share(_files(*names))
    assert m.title == "航海王" and m.year == 1999
    assert m.episode_start == 1 and m.episode_end == 300
    assert m.total_episodes == 300
    assert m.quality == "1080P"
    assert m.media_type == "tv"


# ── 标题匹配 ────────────────────────────────────────────────
def test_title_match_variants():
    assert title_match("The Dark Knight", ["the dark knight"])
    assert title_match("狂飙", ["狂飙 (2023)"])
    assert title_match("四手联弹，两首奏鸣曲", ["四手联弹两首奏鸣曲"])
    assert not title_match("abc", ["xyz"])
    assert not title_match("", ["anything"])


# ── 卡片渲染 ────────────────────────────────────────────────
def _details(**kw):
    base = dict(
        tmdb_id=42, media_type="tv", title="测试剧", original_title="Test Show",
        alt_titles=[], year=2024, release_date="2024-01-01",
        overview="这是概览。" * 50, poster_path="/x.jpg", backdrop_path=None,
        vote_average=8.7, vote_count=100, genres=["剧情", "悬疑"], status="Returning Series",
        cast=["甲", "乙"], countries=["CN"],
        number_of_seasons=2, number_of_episodes=24, creators=[], seasons=[],
    )
    base.update(kw)
    return base


def _plink(code="abc12345", pwd="ef12"):
    url = f"https://115.com/s/{code}" + (f"?password={pwd}" if pwd else "")
    return ParsedLink("115", code, url, pwd)


def test_card_with_match():
    m = analyze_share(_files("测试剧.2024.S01E01.1080p.WEB-DL.mkv"))
    fs = _files("测试剧.2024.S01E01.1080p.WEB-DL.mkv")
    text = card.render_caption(m, _details(), _plink(), fs)
    assert "测试剧" in text and "2024" in text
    assert "✨ 评分：8.7" in text
    assert "💿 画质：" in text and "1080P" in text and "WEB-DL" in text
    assert "🗂️ 季集：" in text and "📋 集数：" in text
    assert "⚙️ 状态：连载中" in text
    assert "https://115.com/s/abc12345?password=ef12" in text
    assert len(text) <= 1024


def test_card_without_match():
    m = analyze_share(_files("神秘资源.2026.mkv"))
    text = card.render_caption(m, None, _plink(pwd=""), _files("神秘资源.2026.mkv"))
    assert "神秘资源" in text
    assert "TMDB 未匹配" in text
    assert "https://115.com/s/abc12345" in text


def test_card_caption_fits_limit():
    names = [f"Show.2024.S01E{i:02d}.1080p.WEB-DL.Some.Release.Group.mkv" for i in range(1, 40)]
    m = analyze_share(_files(*names))
    text = card.render_caption(m, _details(), _plink(), _files(*names))
    assert len(text) <= 1024
    assert "https://115.com/s/abc12345?password=ef12" in text
    assert "</blockquote>" in text  # 链接模块完整保留


def test_card_caption_binary_search_huge_share():
    # 300 文件的大分享:二分截文件项数,仍保住链接
    names = [f"Show.S01E{i:04d}.2024.1080p.WEB-DL.x265-GROUP.mkv" for i in range(1, 301)]
    files = [ShareFile(n, 1024**3, False) for n in names]
    m = analyze_share(files)
    text = card.render_caption(m, _details(), _plink(pwd=""), files)
    assert len(text) <= 1024
    assert "https://115.com/s/abc12345" in text
    assert "已显示前" in text  # 文件清单被截断


def test_card_text_fits_4096():
    names = [f"Show.2024.S01E{i:02d}.1080p.WEB-DL.Some.Release.Group.mkv" for i in range(1, 200)]
    d = _details(overview="超长简介。" * 800)
    text = card.render_text(
        analyze_share(_files(*names)), d, _plink(), _files(*names
    )
    )
    assert len(text) <= 4096
    assert "https://115.com/s/abc12345?password=ef12" in text


def test_card_escapes_html():
    m = analyze_share(_files("Movie.2025.mkv"))
    d = _details(media_type="movie", title="毒<药> & 谎言", overview="<script>x</script>")
    text = card.render_caption(m, d, _plink(pwd=""))
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_card_country_flag():
    m = analyze_share(_files("Movie.2025.mkv"))
    d = _details(media_type="movie", countries=["US", "JP"])
    text = card.render_caption(m, d, _plink(pwd=""))
    assert "🇺🇸 美国" in text and "🇯🇵 日本" in text


# ── 季集范围合并 / TMDB 重分配 ─────────────────────────────
def test_merge_ranges():
    assert card._merge_ranges([1, 2, 3, 5, 6]) == [(1, 3), (5, 6)]
    assert card._merge_ranges([7]) == [(7, 7)]
    assert card._format_ranges([(1, 3), (5, 5)]) == "E01-E03、E05"


def test_reallocate_by_tmdb():
    # TMDB:S1=40集 S2=60集;全局集号 1..100 → 按季范围拆分
    tmdb_seasons = [
        {"season": 1, "episode_count": 40, "name": "S1"},
        {"season": 2, "episode_count": 60, "name": "S2"},
    ]
    out = card._reallocate_by_tmdb(list(range(1, 101)), tmdb_seasons)
    assert out == {1: list(range(1, 41)), 2: list(range(41, 101))}


def test_season_block_reallocates_global_eps():
    # 文件名只有 S01E001-S01E0100(全局集号),TMDB 两季 → 重分配展示
    names = [f"航海王.S01E{i:04d}.1080p.mkv" for i in range(1, 101)]
    m = analyze_share(_files(*names))
    d = _details(
        seasons=[
            {"season": 1, "episode_count": 61, "name": "第1季"},
            {"season": 2, "episode_count": 39, "name": "第2季"},
        ],
        number_of_episodes=100,
    )
    block = card._render_season_block(d, m)
    assert "S01 E01-E61 | S02 E62-E100" in block


def test_file_sort_key_season_dirs():
    files = [
        ShareFile("zeta.mkv", 1, False),
        ShareFile("Season 2", 0, True),
        ShareFile("Season 10", 0, True),
        ShareFile("剧名 {tmdb-123}", 0, True),
        ShareFile("普通目录", 0, True),
    ]
    names = [f.name for f in sorted(files, key=card._file_sort_key)]
    assert names == ["剧名 {tmdb-123}", "普通目录", "Season 2", "Season 10", "zeta.mkv"]


# ── 去重 ────────────────────────────────────────────────────
def test_store_dedup(tmp_path):
    s = Store(tmp_path / "t.db")
    assert not s.is_pushed("c1")
    s.mark_pushed("c1", "标题")
    assert s.is_pushed("c1")
    s.mark_pushed("c1", "新标题")  # 重复标记幂等
    assert s.is_pushed("c1")
    s.close()


# ── 体积格式 ────────────────────────────────────────────────
def test_fmt_size():
    assert fmt_size(500) == "500B"
    assert fmt_size(2048) == "2.00KB"
    assert fmt_size(5 * 1024**3).startswith("5.00GB")


# ── 配置清洗 ────────────────────────────────────────────────
def test_config_placeholder_rejected(tmp_path, capsys):
    import json as _json

    import pytest as _pytest
    p = tmp_path / "config.json"
    p.write_text(_json.dumps({
        "tg_bot_token": "123456:ABC-你的BotToken",  # 中文占位符
        "tg_admin_ids": [1],
        "tmdb_api_key": "你的TMDB v3 key",
    }), encoding="utf-8")
    with _pytest.raises(SystemExit):
        from app.config import load_config
        load_config(p, strict=True)
    out = capsys.readouterr().out
    assert "tg_bot_token" in out
