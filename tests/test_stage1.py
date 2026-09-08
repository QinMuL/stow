"""阶段一单元测试:链接解析 / 文件名解析 / 聚合 / 卡片 / 去重。"""

from __future__ import annotations

from app import card
from app.media import FileInfo, aggregate, title_match
from app.pan115 import ShareLink, fmt_size, parse_links, parse_single
from app.store import Store
from app.tmdb import TmdbMatch


# ── 链接解析 ────────────────────────────────────────────────
def test_parse_url_with_password():
    links = parse_links("看这个 https://115.com/s/abc123xyz?password=ef12")
    assert links == [ShareLink("abc123xyz", "ef12")]


def test_parse_url_tail_token():
    links = parse_links("https://115.com/s/abc123xyz?ef12ef12")
    assert links[0].password == "ef12ef12"


def test_parse_body_access_code():
    links = parse_links("https://115.com/s/abc123xyz 访问码：ef12")
    assert links[0].password == "ef12"


def test_parse_multiple_dedup():
    text = "https://115.com/s/aaa11122?a=1 https://115.com/s/bbb22233 https://115.com/s/aaa11122"
    links = parse_links(text)
    assert [lk.code for lk in links] == ["aaa11122", "bbb22233"]


def test_parse_single_bare_code():
    link = parse_single("abc12345xyz")
    assert link is not None and link.code == "abc12345xyz"


def test_parse_single_ignores_short_words():
    assert parse_single("hello") is None


# ── 文件名解析 ──────────────────────────────────────────────
def test_fileinfo_movie():
    f = FileInfo("The.Dark.Knight.2008.1080p.BluRay.x264.mkv")
    assert f.title == "The Dark Knight"
    assert f.year == 2008
    assert f.season is None
    assert f.resolution == "1080p"


def test_fileinfo_episode():
    f = FileInfo("连载动画.S01E05.2026.2160p.WEB-DL.mkv")
    assert f.season == 1 and f.episode == 5
    assert f.year == 2026
    assert f.resolution == "2160p"


def test_fileinfo_episode_range():
    f = FileInfo("Show.S02E01-E12.1080p.mkv")
    assert f.season == 2 and f.episode == 1


def test_fileinfo_chinese_title():
    f = FileInfo("狂飙.2023.4K.HDR.mkv")
    assert f.title == "狂飙"
    assert f.year == 2023


# ── 聚合 ────────────────────────────────────────────────────
def _files(*names):
    return [(n, 1024, False) for n in names]


def test_aggregate_tv():
    m = aggregate(_files(
        "Show.2024.S01E01.1080p.mkv", "Show.2024.S01E02.1080p.mkv", "Show.2024.S01E03.1080p.mkv",
    ))
    assert m.is_tv
    assert m.title == "Show" and m.year == 2024
    assert m.episodes == {1, 2, 3}
    assert m.resolution == "1080p"
    assert m.seasons == {1}


def test_aggregate_movie_single():
    m = aggregate(_files("Movie.Name.2025.2160p.mkv"))
    assert not m.is_tv
    assert m.title == "Movie Name"


def test_aggregate_drops_non_video():
    m = aggregate(_files("Movie.2025.mkv", "poster.jpg", "info.nfo"))
    assert len(m.video_files) == 1
    assert m.file_count == 3


# ── 标题匹配 ────────────────────────────────────────────────
def test_title_match_variants():
    assert title_match("The Dark Knight", "the dark knight")
    assert title_match("狂飙", "狂飙 (2023)")
    assert not title_match("abc", "xyz")


# ── 卡片渲染 ────────────────────────────────────────────────
def _match(**kw):
    base = dict(
        tmdb_id=42, media_type="tv", title="测试剧", year=2024,
        poster_path="/x.jpg", rating=8.7, overview="这是概览。" * 50,
        genres=["剧情", "悬疑"], runtime_min=None, seasons=2, episodes=24,
    )
    base.update(kw)
    return TmdbMatch(**base)


def test_card_with_match():
    m = aggregate(_files("测试剧.2024.S01E01.1080p.mkv"))
    text = card.render(m, _match(), ShareLink("abc12345", "ef12"))
    assert "测试剧" in text and "(2024)" in text
    assert "★ 8.7" in text
    assert "1080P" in text
    assert "https://115.com/s/abc12345?password=ef12" in text
    assert "<code>ef12</code>" in text
    assert len(text) <= 1024


def test_card_without_match():
    m = aggregate(_files("神秘资源.2026.mkv"))
    text = card.render(m, None, ShareLink("abc12345"))
    assert "神秘资源" in text
    assert "TMDB 未匹配" in text
    assert "https://115.com/s/abc12345</code>" not in text  # 无码链接纯文本


def test_card_caption_fits_limit():
    names = [f"Show.2024.S01E{i:02d}.1080p.WEB-DL.Some.Release.Group.mkv" for i in range(1, 40)]
    m = aggregate(_files(*names))
    text = card.render(m, _match(), ShareLink("abc12345", "ef12"))
    assert len(text) <= 1024
    assert text.endswith("https://115.com/s/abc12345?password=ef12")


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
        "tg_chat_id": "-100123",
        "tg_admin_ids": [1],
        "tmdb_api_key": "你的TMDB v3 key",
    }), encoding="utf-8")
    with _pytest.raises(SystemExit):
        from app.config import load_config
        load_config(p)
    out = capsys.readouterr().out
    assert "tg_bot_token" in out
