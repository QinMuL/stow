"""目录标准化器单测:季目录识别 / 资源名构建 / 垃圾标题判定。"""

from app.normalizer import (
    _suspect_title,
    build_resource_name,
    format_season_dir,
    parse_season_dir,
)


def test_parse_season_dir_forms():
    assert parse_season_dir("Season 1") == 1
    assert parse_season_dir("S02") == 2
    assert parse_season_dir("第3季") == 3
    assert parse_season_dir("第一季") == 1
    assert parse_season_dir("01") == 1
    assert parse_season_dir("12") == 12
    assert parse_season_dir("Extras") is None
    assert parse_season_dir("怒火救援") is None


def test_parse_season_dir_chinese_numbers():
    assert parse_season_dir("第十一季") == 11
    assert parse_season_dir("第二十季") == 20


def test_parse_season_dir_bare_chinese():
    # 裸中文数字季目录(当前标准格式)必须可识别,保证 normalize 幂等
    for n, name in ((1, "一"), (3, "三"), (10, "十"), (11, "十一"), (20, "二十"), (23, "二十三")):
        assert parse_season_dir(name) == n
    # 非季目录名不误判
    assert parse_season_dir("三体") is None
    assert parse_season_dir("十面埋伏") is None


def test_format_parse_roundtrip():
    # 全范围 round-trip:生成 ↔ 识别 幂等闭环(防牵连 bug)
    for n in list(range(1, 30)) + [47, 99]:
        assert parse_season_dir(format_season_dir(n)) == n


def test_format_and_build():
    assert format_season_dir(1) == "一"
    assert format_season_dir(3) == "三"
    assert format_season_dir(10) == "十"
    assert format_season_dir(11) == "十一"
    assert format_season_dir(20) == "二十"
    assert format_season_dir(21) == "二十一"
    assert build_resource_name("葬送的芙莉莲", 2026, 246389) == "葬送的芙莉莲 (2026) {tmdb-246389}"
    assert build_resource_name("沙丘", None, 693134) == "沙丘 {tmdb-693134}"


def test_suspect_title_detection():
    # 分享码/时间戳形态 → 垃圾标题
    assert _suspect_title("swseiuq3znw_20260910_213100")
    assert _suspect_title("swsf58b3hib 20260910 200856")
    # 正常标题 → 非垃圾
    assert not _suspect_title("怒火救援")
    assert not _suspect_title("The Movie 2023")
    assert not _suspect_title("")


def test_detect_media_season_voting_fallback(tmp_path, monkeypatch):
    """文件名全是 Season 1.SxxExx 形态时,标题应回退目录名(玲音 (1998))。"""
    from app.normalizer import ShareNormalizer
    from app.pan115 import Pan115Reader

    r = Pan115Reader.__new__(Pan115Reader)  # 不触网:_detect_media 不用 client
    n = ShareNormalizer(r, tmdb=None)
    items = [
        {"name": f"Season 1.S01E0{i}.mkv", "size": 1, "is_dir": False} for i in range(1, 8)
    ]
    media = n._detect_media("玲音 (1998)", items)
    assert media is not None
    assert media.title == "玲音" and media.year == 1998
    assert media.media_type == "tv"
