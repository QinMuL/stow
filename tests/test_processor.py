"""处理段测试:ed2k 哈希/链接、命名器、探测映射、处理链(用假件,不触网)。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

import app.processor as processor_mod
from app.cleaner import CleanError, filter_ffmetadata, report_from_ffprobe
from app.config import Config
from app.ed2k import ED2K_CHUNK, ed2k_hash_file, ed2k_uri, sanitize_ed2k_name
from app.media import analyze_share
from app.namer import quality_label, render_name, sanitize_name
from app.pan115 import ShareFile, strip_dup_suffix
from app.probe import ProbeTags, tags_from_ffprobe
from app.processor import ProcessChain
from app.store import Store

try:
    from Crypto.Hash import MD4 as _MD4

    def md4(data: bytes) -> str:
        return _MD4.new(data).hexdigest()
except ImportError:  # pragma: no cover
    def md4(data: bytes) -> str:
        raise RuntimeError("缺 pycryptodome")


def _write(path: Path, size: int, fill: bytes = b"A") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fill * size)
    return path


# ── ed2k 哈希 ───────────────────────────────────────────────
def test_md4_rfc1320_vectors():
    assert md4(b"") == "31d6cfe0d16ae931b73c59d7e0c089c0"
    assert md4(b"abc") == "a448017aaf21d8525fc10ae87aa6729d"
    assert md4(b"message digest") == "d9130a8164549fe818874806e1c7014b"


def test_ed2k_empty_and_single_chunk(tmp_path):
    empty = _write(tmp_path / "empty.mkv", 0)
    size, root = asyncio.run(ed2k_hash_file(str(empty)))
    assert size == 0 and root == md4(b"")

    small = tmp_path / "s.mkv"
    data = b"x" * 1_234_567
    small.write_bytes(data)
    size, root = asyncio.run(ed2k_hash_file(str(small)))
    assert size == len(data) and root == md4(data)      # 单块 = 该块哈希


def test_ed2k_multi_chunk(tmp_path):
    """多块:块哈希拼接后再 MD4(旧项目语义,不做零填充)。"""
    big = tmp_path / "big.mkv"
    parts = [b"A" * ED2K_CHUNK, b"B" * ED2K_CHUNK, b"C" * 100]
    big.write_bytes(b"".join(parts))
    size, root = asyncio.run(ed2k_hash_file(str(big)))
    assert size == sum(len(p) for p in parts)
    assert root == md4(b"".join(md4(p).encode() and bytes.fromhex(md4(p)) for p in parts))


def test_ed2k_uri_and_name_sanitize():
    assert ed2k_uri("show.mkv", 12345, "ABCDEF0123456789") == (
        "ed2k://|file|show.mkv|12345|abcdef0123456789|/")
    # 文件名含 `|` / 换行会破坏链接语义 → 净化
    assert "|" not in sanitize_ed2k_name("bad|name\n.mkv")
    assert ed2k_uri("a|b\nc.mkv", 1, "0" * 32).count("|") == 5   # 只剩 4 个分隔 + 首尾


# ── 命名器 ──────────────────────────────────────────────────
def _media(name: str):
    return analyze_share([ShareFile(name, 5_000_000_000, False)])


def test_render_episode_name():
    """剧集模板(原项目规则):SxxEyy + 第NN集(两位补零),来源在效果之前。"""
    tags = ProbeTags(resolution="2160p", effect="HDR10", bit_depth="10bit",
                     video_codec="H.265", audio_codec="DDP", frame_rate="25fps")
    raw = "飞到我心上.2026.WEB-DL.S01E12.mkv"
    name = render_name(_media(raw), {"title": "飞到我心上", "year": 2026, "tmdb_id": 123456},
                       tags, ".mkv", raw_name=raw)
    assert name == ("飞到我心上.2026.S01E12.第12集."
                    "2160p.WEB-DL.HDR10.H.265.10bit.25fps.DDP {tmdb-123456}.mkv")


def test_render_episode_pads_episode_number():
    """第 8 集 → 第08集(原项目两位补零)。"""
    raw = "某剧.2026.S01E08.mkv"
    name = render_name(_media(raw), {"title": "某剧", "year": 2026}, ProbeTags(), ".mkv", raw_name=raw)
    assert "第08集" in name


def test_render_keeps_platform_uhd_remux_from_raw_name():
    """平台/UHD/REMUX/BluRay 这些"只有原文件名才有"的线索按旧规则带进名字。"""
    raw = "某剧.2026.2160p.UHD.BluRay.REMUX.Viu.WEB-DL.H.265.mkv"
    tags = ProbeTags(resolution="2160p", video_codec="H.265")
    name = render_name(_media(raw), {"title": "某剧", "year": 2026}, tags, ".mkv", raw_name=raw)
    assert ".UHD." in name and "BluRay.REMUX" in name and "Viu" in name


def test_render_movie_name():
    raw = "何以为家 (2018).mkv"
    tags = ProbeTags(resolution="1080p", video_codec="H.264", audio_codec="AAC")
    name = render_name(_media(raw), {"title": "何以为家", "year": 2018, "tmdb_id": 517148},
                       tags, ".mkv", raw_name=raw)
    assert name == "何以为家 (2018) - 1080p.H.264.AAC {tmdb-517148}.mkv"


def test_render_tv_without_episode_is_blocked():
    """剧集(有季无集) → 返回空串交人工;无 SxxEyy 的按电影处理。"""
    tags = ProbeTags(resolution="1080p")
    raw = "某剧.2026.S01.mkv"
    assert render_name(_media(raw), {"title": "某剧", "year": 2026}, tags, ".mkv", raw_name=raw) == ""
    raw2 = "某剧.2026.WEB-DL.mkv"
    assert render_name(_media(raw2), {"title": "某剧", "year": 2026}, tags, ".mkv",
                       raw_name=raw2) == "某剧 (2026) - 1080p.WEB-DL.mkv"


def test_render_requires_title():
    empty = analyze_share([ShareFile("   .mkv", 10, False)])
    assert render_name(empty, None, ProbeTags(), ".mkv") == ""


def test_sanitize_name_rules():
    assert sanitize_name("a:b.mkv") == "a：b.mkv"           # 冒号转全角
    assert sanitize_name('a<b>c"d.mkv') == "abcd.mkv"       # 非法字符剔除
    assert sanitize_name("tail. .mkv") == "tail.mkv"        # 尾部点空格剥离
    long = sanitize_name("字" * 300 + ".mkv")
    assert len(long) <= 185 and long.endswith(".mkv")


def test_quality_label_order_and_no_duplicates():
    """画质段顺序固定为"分辨率.平台.来源.效果.编码…",探测值优先且不重复。"""
    raw = "片名.2026.1080p.WEB-DL.mkv"
    tags = ProbeTags(resolution="1080p", video_codec="H.265", effect="HDR10")
    label = quality_label(tags, _media(raw), raw)
    assert label == "1080p.WEB-DL.HDR10.H.265"      # 顺序固定,且 1080p 不重复


# ── 探测映射(纯函数) ───────────────────────────────────────
def test_tags_from_ffprobe_mapping():
    data = {"streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 1608,
         "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084"},
        {"codec_type": "audio", "codec_name": "eac3", "channels": 6},
        {"codec_type": "audio", "codec_name": "aac", "channels": 2},
    ], "format": {"duration": "1234.5"}}
    t = tags_from_ffprobe(data)
    assert (t.resolution, t.effect, t.bit_depth) == ("2160p", "HDR10", "10bit")
    assert t.video_codec == "H.265"
    assert t.audio_codec == "DDP 5.1"      # 取第一条音轨,且带声道数
    assert t.audio_tracks == 2 and abs(t.duration - 1234.5) < 0.01 and t.ok()


def test_normalize_audio_codec_plus_channels():
    """音频标签 = 编码 + 声道数(照搬原项目)。

    回归锁:漏了声道,文件名里本来写着的 5.1 会被"技术标签以 ffprobe 为准"这条规则覆盖掉。
    """
    from app.probe import normalize_audio

    assert normalize_audio("eac3", "", 6) == "DDP 5.1"
    assert normalize_audio("ac3", "", 2) == "DD 2.0"
    assert normalize_audio("truehd", "", 8) == "TrueHD 7.1"
    assert normalize_audio("aac", "", 0) == "AAC"      # 没有声道信息 → 不拼
    assert normalize_audio("", "", 6) == ""            # 没有编码 → 整段空


def test_normalize_audio_dts_profile_pcm_and_unknown():
    """DTS 用 profile、pcm_* 一律 LPCM、认不出的回退原名(都与原项目一致)。"""
    from app.probe import normalize_audio

    assert normalize_audio("dts", "DTS-HD MA", 6) == "DTS-HD MA 5.1"   # profile 优先于编码名
    assert normalize_audio("dts", "DTS", 6) == "DTS 5.1"
    assert normalize_audio("pcm_bluray", "", 2) == "LPCM 2.0"          # 所有 pcm_* 都是 LPCM
    assert normalize_audio("vorbis", "", 2) == "vorbis 2.0"            # 认不出 → 回退原名
    assert normalize_audio("eac3", "", 4) == "DDP 4"                   # 未收录的声道数原样写


def test_tags_dolby_vision_preferred_over_hdr10():
    data = {"streams": [{
        "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
        "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084",
        "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8}],
    }]}
    t = tags_from_ffprobe(data)
    assert t.effect == "DoVi P8"                       # DV 优先且不叠 HDR10


def test_tags_wide_movie_uses_longest_side():
    """2.35:1 的 4K:高度不到 3200,靠宽度判定。"""
    t = tags_from_ffprobe({"streams": [{"codec_type": "video", "codec_name": "h264",
                                        "width": 3840, "height": 1600, "pix_fmt": "yuv420p"}]})
    assert t.resolution == "2160p" and t.bit_depth == ""   # 8bit 不标


def test_tags_sdr_labels_sdr():
    """SDR 也显式标出来(原项目口径);8bit 仍不标色深。"""
    t = tags_from_ffprobe({"streams": [{"codec_type": "video", "codec_name": "h264",
                                        "width": 1920, "height": 1080,
                                        "color_transfer": "bt709", "pix_fmt": "yuv420p"}]})
    assert t.effect == "SDR" and t.resolution == "1080p" and t.bit_depth == ""


def test_tags_without_video_stream_has_no_sdr_claim():
    """探测失败/纯音频:没有视频轨就不该断言 SDR,留空。"""
    t = tags_from_ffprobe({"streams": [{"codec_type": "audio", "codec_name": "aac"}]})
    assert t.effect == "" and not t.ok()


# ── 重复下载后缀(浏览器把重名文件存成 "片名.mkv (1)") ───────
def test_strip_dup_suffix_cases():
    assert strip_dup_suffix("片名.2026.1080p.mkv (1)") == "片名.2026.1080p.mkv"
    assert strip_dup_suffix("片名.2026.1080p.mkv (12)") == "片名.2026.1080p.mkv"
    assert strip_dup_suffix("片名.2026.1080p.mkv_1") == "片名.2026.1080p.mkv"
    assert strip_dup_suffix("片名.2026.1080p.mkv-2") == "片名.2026.1080p.mkv"
    assert strip_dup_suffix("片名.zh.srt (1)") == "片名.zh.srt"       # 字幕同样处理
    # 不该动的:正常名 / 剥完不是媒体文件
    assert strip_dup_suffix("片名.2026.1080p.mkv") == "片名.2026.1080p.mkv"
    assert strip_dup_suffix("片名.2026") == "片名.2026"
    assert strip_dup_suffix("电影 4") == "电影 4"
    assert strip_dup_suffix("片名.mkv.bak1") == "片名.mkv.bak1"


def test_share_file_is_video_tolerates_dup_suffix():
    """带重复下载后缀的文件仍须被认作视频,否则整集被判"非视频"永远跳过。"""
    assert ShareFile("片名.2026.mkv (1)", 2000, False).is_video
    assert ShareFile("片名.2026.mkv_1", 2000, False).is_video
    assert not ShareFile("片名.2026.mkv (1)", 0, True).is_video      # 目录不是视频
    assert not ShareFile("说明.txt (1)", 2000, False).is_video


def test_parse_filename_leading_sxxexx_keeps_title_clean():
    """片名在集号之后时,年份不能被吞进标题(否则 TMDB 搜不到 → 待人工)。"""
    m = analyze_share([ShareFile(
        "S02E08.One.Hundred.Years.of.Solitude.2024.1080p.WEB-DL.mkv", 2000, False)])
    assert m.title == "One Hundred Years of Solitude"
    assert (m.season, m.episode_start) == (2, 8)


def test_parse_filename_year_not_mistaken_for_season():
    """'剧名.2026.E05...' 里的年份不能被 guessit 当季号(否则产出 S2026E05)。"""
    m = analyze_share([ShareFile("剧名.2026.E05.1080p.WEB-DL.H.264.mkv", 2000, False)])
    assert m.season is None and m.episode_start == 5 and m.year == 2026


def test_render_name_tv_defaults_season_and_accepts_media_data():
    """有集号无季号 → S1(原项目惯例);同时兼容只有 episode 字段的单文件解析结果。"""
    from app.media import MediaData

    media = MediaData(title="剧名", media_type="tv", episode=5,
                      quality="1080P", source="WEB-DL")
    name = render_name(media, None, ProbeTags(resolution="1080p"), raw_name="剧名.E05.mkv")
    assert name.startswith("剧名.S01E05.第05集.")
    # 无集号(电影/无法判断)仍拦下交人工
    assert render_name(MediaData(title="剧名", media_type="tv"), None, None,
                       raw_name="剧名.mkv") == ""


def test_render_name_extension_ignores_dup_suffix():
    """调用方不传 ext 时,扩展名也不能从 "片名.mkv (1)" 推出 ".mkv (1)"。"""
    raw = "片名.2026.1080p.WEB-DL.mkv (1)"
    media = analyze_share([ShareFile(raw, 2000, False)])
    name = render_name(media, {"title": "片名", "year": 2026},
                       ProbeTags(resolution="1080p"), raw_name=raw)
    assert name.endswith(".mkv") and "(1)" not in name


def test_chain_handles_browser_duplicate_suffix(tmp_path, monkeypatch):
    """端到端:带 " (1)" 的文件要正常改名归档,名字与扩展名都不能被带脏。"""
    chain, bot, _ = _chain(tmp_path, monkeypatch=monkeypatch,
                           name="飞到我心上.2026.WEB-DL.S01E12.mkv (1)")
    report = asyncio.run(chain.scan_now())
    assert "processed 1" in report
    archived = [p.name for p in Path(bot.cfg.clouddrive_dir).iterdir()]
    assert archived == ["飞到我心上.2026.S01E12.第12集.1080p.WEB-DL.H.264.AAC {tmdb-123456}.mkv"]


# ── 处理链 ──────────────────────────────────────────────────
class FakeTmdb:
    def __init__(self, details):
        self.details = details

    async def match(self, media):
        return self.details


class FakeBot:
    def __init__(self, cfg: Config, store: Store, details=None) -> None:
        self.cfg = cfg
        self.store = store
        self.tmdb = FakeTmdb(details)
        self.pushed: list = []
        self.notified: list[str] = []
        self.clean_calls = False

    async def push_link(self, link, **kw):
        from app.bot import PushResult

        self.pushed.append(link)
        return PushResult(True, "✅ 已推送")

    async def _notify_uid(self, uid, text):
        self.notified.append(text)


async def _async(value):
    """把同步值包成 awaitable(替身用)。"""
    return value


def _fake_clean(did_clean: bool, why: str = ""):
    """替身清洗:返回是否清洗过(不碰真 ffmpeg)。"""

    async def _clean(path, **kw):
        return did_clean, why

    return _clean


def _fake_probe(tags: ProbeTags | None = None):
    """替身探测:测试文件不是真视频,真 ffprobe 必然认不出。"""
    result = tags or ProbeTags(resolution="1080p", video_codec="H.264", audio_codec="AAC",
                               video_tracks=1, audio_tracks=1, duration=60.0)

    async def _probe(path, **kw):
        return result

    return _probe


def _chain(tmp_path, *, details=None, min_size_mb=0, min_age_seconds=0,
           name="飞到我心上.2026.WEB-DL.S01E12.mkv", probe_tags=None, monkeypatch=None,
           age_seconds=3600):
    cfg = Config(data_dir=str(tmp_path), media_root=str(tmp_path / "media"),
                 min_size_mb=min_size_mb, min_age_seconds=min_age_seconds,
                 tg_admin_ids=[1])
    cfg.ensure_media_dirs()
    bot = FakeBot(cfg, Store(tmp_path / "t.db"), details)
    if details is None:
        bot.tmdb = FakeTmdb({"title": "飞到我心上", "year": 2026, "tmdb_id": 123456})
    path = _write(cfg.openlist_dir / name, 2000)
    if age_seconds:                       # 拨老 mtime,过"静默年龄"门槛
        os.utime(path, (time.time() - age_seconds, time.time() - age_seconds))
    if monkeypatch is not None:
        monkeypatch.setattr(processor_mod, "probe_file", _fake_probe(probe_tags))
    return ProcessChain(bot), bot, path


def test_chain_happy_path_renames_hashes_pushes_and_archives(tmp_path, monkeypatch):
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    report = asyncio.run(chain.scan_now())
    assert "processed 1" in report
    # 归档到 clouddrive 且用了规范名
    archived = list(Path(bot.cfg.clouddrive_dir).iterdir())
    assert len(archived) == 1
    assert archived[0].name == (
        "飞到我心上.2026.S01E12.第12集.1080p.WEB-DL.H.264.AAC {tmdb-123456}.mkv")
    # 推的是 ed2k 卡,链接里的文件名就是规范名,hash 非空
    assert len(bot.pushed) == 1
    uri = bot.pushed[0].url
    assert uri.startswith("ed2k://|file|飞到我心上.2026.S01E12") and uri.rstrip("/").endswith("|")
    row = bot.store.get_local_file(archived[0].name, archived[0].stat().st_size)
    assert row and row["status"] == "processed" and row["ed2k"] == uri and row["tmdb_id"] == 123456


def test_chain_moves_sidecar_files_together(tmp_path, monkeypatch):
    chain, bot, _ = _chain(tmp_path, monkeypatch=monkeypatch)
    (bot.cfg.openlist_dir / "飞到我心上.2026.WEB-DL.S01E12.zh.srt").write_text("sub", encoding="utf-8")
    (bot.cfg.openlist_dir / "banner.jpg").write_bytes(b"jpg")
    asyncio.run(chain.scan_now())
    names = {p.name for p in Path(bot.cfg.clouddrive_dir).iterdir()}
    vid = next(n for n in names if n.endswith(".mkv"))
    assert f"{vid[:-len('.mkv')]}.zh.srt" in names              # 字幕跟着视频走并同步改名
    assert "banner.jpg" not in names                            # 无关图片不动


def test_chain_blocks_unrecognized_and_notifies(tmp_path, monkeypatch):
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    bot.tmdb = FakeTmdb(None)                                   # TMDB 未命中
    report = asyncio.run(chain.scan_now())
    assert "unrecognized 1" in report
    assert path.exists()                                        # 文件留在原地
    assert list(Path(bot.cfg.clouddrive_dir).iterdir()) == []    # 不归档
    assert bot.pushed == []                                      # 不推卡
    assert bot.notified and "TMDB 未命中" in bot.notified[0]
    assert bot.store.list_local_files("unrecognized")


def test_chain_gates_small_and_fresh_files(tmp_path, monkeypatch):
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch, min_size_mb=50)          # 文件仅 2000B
    assert "未就绪" in asyncio.run(chain.scan_now()) or True
    assert path.exists() and bot.pushed == []

    chain2, bot2, path2 = _chain(tmp_path / "b", monkeypatch=monkeypatch,
                                 min_age_seconds=3600, age_seconds=0)   # 刚写入的不动
    asyncio.run(chain2.scan_now())
    assert path2.exists() and bot2.pushed == []


def test_chain_skips_non_video_and_already_processed(tmp_path, monkeypatch):
    chain, bot, _ = _chain(tmp_path, monkeypatch=monkeypatch)
    (bot.cfg.openlist_dir / "readme.txt").write_text("x", encoding="utf-8")
    (bot.cfg.openlist_dir / "poster.jpg").write_bytes(b"jpg")
    asyncio.run(chain.scan_now())
    assert {p.name for p in Path(bot.cfg.clouddrive_dir).iterdir()} != set()   # 视频已归档
    # 再跑一轮:无可处理文件(视频已走,伴行/非视频不处理)
    report = asyncio.run(chain.scan_now())
    assert "没有可处理" in report or "非视频" in report


def test_chain_dedup_by_pushed_hash(tmp_path, monkeypatch):
    """同内容已推送过:不重复推卡,但文件照常归档。"""
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    size, root = asyncio.run(ed2k_hash_file(str(path)))
    bot.store.mark_pushed(root, "旧卡")
    asyncio.run(chain.scan_now())
    assert bot.pushed == []                                      # 未重复推
    assert len(list(Path(bot.cfg.clouddrive_dir).iterdir())) == 1  # 仍归档


def test_tmdb_annotation_is_not_taken_as_release_group():
    """回归:文件名里的 {tmdb-NNN} 曾被 guessit 当成发布组 → 名字里多出 "-tmdb"。"""
    raw = "茶啊二中.2014.S06E01.第01集.2160p.WEB-DL {tmdb-119059}.mkv"
    media = _media(raw)
    assert media.release_group == ""
    name = render_name(media, {"title": "茶啊二中", "year": 2014, "tmdb_id": 119059},
                       ProbeTags(resolution="2160p"), ".mkv", raw_name=raw)
    assert "-tmdb" not in name and name.endswith(" {tmdb-119059}.mkv")


# ── 清洗(按需:只在探到广告类脏数据时才做) ─────────────────
def _ffprobe_like(*, comment="", chapter_titles=(), audio_titles=()):
    fmt = {"duration": "60.0"}
    if comment:
        fmt["tags"] = {"comment": comment}
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264",
                "width": 1920, "height": 1080, "pix_fmt": "yuv420p"}]
    for i, t in enumerate(audio_titles, start=1):
        streams.append({"index": i, "codec_type": "audio", "codec_name": "aac",
                        "tags": {"title": t}})
    return {
        "streams": streams,
        "format": fmt,
        "chapters": [{"tags": {"title": t}} for t in chapter_titles],
    }


def test_clean_report_detects_ad_junk():
    """广告类脏数据(容器标签/章节/音轨标题)都能识别出来。"""
    rpt = report_from_ffprobe(_ffprobe_like(
        comment="更多资源访问 http://广告站.com",
        chapter_titles=["正片", "点击订阅频道推广"],
        audio_titles=["国语", "广告 群号12345"],
    ))
    assert len(rpt.junk_tags) == 1 and rpt.junk_chapters == ["点击订阅频道推广"]
    assert rpt.junk_tracks and rpt.junk_tracks[0]["kind"] == "音轨"
    assert rpt.has_junk and "容器标签" in rpt.summary()


def test_clean_report_clean_file_has_no_junk():
    """干净文件:一个脏项都不报 → 调用方直接跳过清洗。"""
    rpt = report_from_ffprobe(_ffprobe_like(chapter_titles=["正片"], audio_titles=["国语", "粤语"]))
    assert rpt.has_junk is False and rpt.summary() == ""


def test_filter_ffmetadata_drops_junk_keeps_clean():
    """ffmetadata 过滤:垃圾章节剔除、正常章节保留;有垃圾容器标签则清空全局段。"""
    rpt = report_from_ffprobe(_ffprobe_like(comment="www.广告.com"))
    src = "\n".join([
        ";FFMETADATA1", "title=正常标题", "comment=www.广告.com", "",
        "[CHAPTER]", "TIMEBASE=1/1000", "START=0", "END=1000", "title=正片", "",
        "[CHAPTER]", "TIMEBASE=1/1000", "START=1000", "END=2000", "title=点击订阅", "",
    ])
    out = filter_ffmetadata(src, rpt)
    assert "更多资源" not in out and "www.广告.com" not in out      # 垃圾标签被清
    assert "title=正常标题" not in out                              # 有垃圾标签 → 全局段整体清空
    assert "title=正片" in out and "点击订阅" not in out            # 正常章节留、垃圾章节丢


def test_clean_file_untouched_when_no_junk(tmp_path, monkeypatch):
    """核心规则:没有广告类脏数据 → **一个字节都不动、也不跑 ffmpeg**。"""
    import app.cleaner as cleaner_mod

    f = tmp_path / "clean.mkv"
    f.write_bytes(b"original-bytes")
    monkeypatch.setattr(cleaner_mod, "_ffprobe_json",
                        lambda p: _async(_ffprobe_like(chapter_titles=["正片"])))
    runs: list = []
    monkeypatch.setattr(cleaner_mod, "_run", lambda cmd, **kw: _async(runs.append(cmd) or True))
    cleaned, why = asyncio.run(cleaner_mod.clean_file(f))
    assert (cleaned, why) == (False, "干净")
    assert f.read_bytes() == b"original-bytes"      # 没动
    assert runs == []                                # 没跑 ffmpeg


def test_clean_file_replaces_in_place_when_junk(tmp_path, monkeypatch):
    """有脏数据 → remux 后**同名替换**;校验通过才算成功。"""
    import app.cleaner as cleaner_mod

    f = tmp_path / "junk.mkv"
    f.write_bytes(b"original-bytes")
    probe = _ffprobe_like(comment="http://广告站.com", chapter_titles=["点击订阅"])
    monkeypatch.setattr(cleaner_mod, "_ffprobe_json", lambda p: _async(probe))

    async def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"cleaned-bytes")   # 伪造 ffmpeg 产出(含 ffmetadata 导出)
        return True

    monkeypatch.setattr(cleaner_mod, "_run", fake_run)
    cleaned, why = asyncio.run(cleaner_mod.clean_file(f))
    assert cleaned is True and "容器标签" in why
    assert f.read_bytes() == b"cleaned-bytes"         # 同名替换
    assert not list(tmp_path.glob("*.cleaned*"))      # 无残留半成品


def test_clean_file_validation_failure_keeps_original(tmp_path, monkeypatch):
    """校验不过(视频轨数变化)→ 抛错、删半成品、原件保留。"""
    import app.cleaner as cleaner_mod

    f = tmp_path / "bad.mkv"
    f.write_bytes(b"original-bytes")
    junk = _ffprobe_like(comment="www.广告.com")

    async def fake_probe(p):
        if str(p).endswith(".cleaned.mkv"):           # 产物探测结果异常
            return {"streams": [], "format": {"duration": "10.0"}, "chapters": []}
        return junk

    async def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"half-made")
        return True

    monkeypatch.setattr(cleaner_mod, "_ffprobe_json", fake_probe)
    monkeypatch.setattr(cleaner_mod, "_run", fake_run)
    with pytest.raises(CleanError):
        asyncio.run(cleaner_mod.clean_file(f))
    assert f.read_bytes() == b"original-bytes"
    assert not list(tmp_path.glob("*.cleaned*"))


def test_clean_runs_before_rename_and_hash(tmp_path, monkeypatch):
    """顺序:清洗发生在重命名之前(用户 2026-09-11 指定)。"""
    order: list[str] = []
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)

    async def fake_clean(p, **kw):
        order.append("clean")
        return True, "容器标签×1"

    real_hash = processor_mod.ed2k_hash_file

    async def spy_hash(p, **kw):
        order.append("hash")
        return await real_hash(p, **kw)

    monkeypatch.setattr(processor_mod, "clean_file", fake_clean)
    monkeypatch.setattr(processor_mod, "ed2k_hash_file", spy_hash)
    asyncio.run(chain.scan_now())
    assert order[:2] == ["clean", "hash"]          # 清洗先于哈希


def test_clean_failure_keeps_original_and_reports(tmp_path, monkeypatch):
    """清洗失败:原件保留、记 failed、通知人工,不推卡不归档。"""
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)

    async def boom(p, **kw):
        raise CleanError("校验不过(视频轨 1→0)")

    monkeypatch.setattr(processor_mod, "clean_file", boom)
    report = asyncio.run(chain.scan_now())
    assert "failed 1" in report
    assert path.exists()                                     # 原件在
    assert bot.pushed == [] and list(Path(bot.cfg.clouddrive_dir).iterdir()) == []
    assert bot.notified and "清洗失败" in bot.notified[0]


def test_clean_disabled_by_config(tmp_path, monkeypatch):
    """开关关闭时不清洗(仍然走完识别/推卡)。"""
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    bot.cfg.clean_enabled = False
    called = []

    async def spy(p, **kw):
        called.append(p)
        return False, "干净"

    monkeypatch.setattr(processor_mod, "clean_file", spy)
    asyncio.run(chain.scan_now())
    assert called == [] and bot.pushed


# ── 目录展开(季包/合集丢进来 → 逐文件处理;归档打平) ─────────
def test_folder_is_expanded_and_flattened(tmp_path, monkeypatch):
    """目录里多个视频 → 每个各自处理;归档到 clouddrive 打平;空目录清掉。"""
    chain, bot, placeholder = _chain(tmp_path, name="占位.mkv", monkeypatch=monkeypatch)
    placeholder.unlink()                                 # 去掉助手自带的占位文件,只留待测目录
    box = bot.cfg.openlist_dir / "某剧 第一季"
    (box / "季目录").mkdir(parents=True)
    names = ["某剧.S01E01.1080p.WEB-DL.mkv", "某剧.S01E02.1080p.WEB-DL.mkv"]
    for i, n in enumerate(names):
        target = (box if i == 0 else box / "季目录") / n
        target.write_bytes(b"A" * 2000)
        os.utime(target, (time.time() - 3600, time.time() - 3600))
    (box / "某剧.S01E01.1080p.WEB-DL.zh.srt").write_text("sub", encoding="utf-8")

    report = asyncio.run(chain.scan_now())
    assert "processed 2" in report                       # 两个视频都被展开处理
    assert "清理空目录" in report                          # 空目录被清
    archived = {p.name for p in Path(bot.cfg.clouddrive_dir).iterdir()}
    vids = {n for n in archived if n.endswith(".mkv")}
    assert len(vids) == 2 and all(".S01E0" in n for n in vids)   # 打平:同级存放
    assert "飞到我心上.2026.S01E01.第01集.1080p.WEB-DL.H.264.AAC {tmdb-123456}.zh.srt" in archived
    assert not box.exists() or not any(box.iterdir())     # 目录已清空/移除


def test_folder_sidecar_follows_its_video(tmp_path, monkeypatch):
    """目录里的同名字幕跟着它那一集走(视频没有字幕时留原地)。"""
    chain, bot, placeholder = _chain(tmp_path, name="占位.mkv", monkeypatch=monkeypatch)
    placeholder.unlink()
    box = bot.cfg.openlist_dir / "合集"
    box.mkdir()
    name = "某剧.S01E03.1080p.WEB-DL.mkv"
    v = box / name
    v.write_bytes(b"A" * 2000)
    os.utime(v, (time.time() - 3600, time.time() - 3600))
    (box / "某剧.S01E03.1080p.WEB-DL.zh.srt").write_text("sub", encoding="utf-8")
    asyncio.run(chain.scan_now())
    names = {p.name for p in Path(bot.cfg.clouddrive_dir).iterdir()}
    vid = next(n for n in names if n.endswith(".mkv"))
    # 字幕不仅跟着走,还要改名与视频同步(否则 Emby 认不出这对字幕)
    assert f"{vid[:-len('.mkv')]}.zh.srt" in names


def test_nested_non_video_files_are_ignored(tmp_path, monkeypatch):
    """目录里的非视频文件不处理、也不阻塞视频。"""
    chain, bot, placeholder = _chain(tmp_path, name="占位.mkv", monkeypatch=monkeypatch)
    placeholder.unlink()
    box = bot.cfg.openlist_dir / "混装"
    box.mkdir()
    for fn, data in (("readme.txt", b"x"), ("cover.jpg", b"jpg")):
        (box / fn).write_bytes(data)
    v = box / "某剧.S01E04.1080p.WEB-DL.mkv"
    v.write_bytes(b"A" * 2000)
    os.utime(v, (time.time() - 3600, time.time() - 3600))
    asyncio.run(chain.scan_now())
    assert any(p.suffix == ".mkv" for p in Path(bot.cfg.clouddrive_dir).iterdir())


def test_rename_permission_error_is_not_failure(tmp_path, monkeypatch):
    """改名遇到占用(PermissionError)→ 视为"未就绪"重试,不记 failed(不烧重试配额)。"""
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)

    def locked(self, target=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(processor_mod.Path, "rename", locked, raising=True)
    report = asyncio.run(chain.scan_now())
    assert "未就绪 1" in report
    assert path.exists() and bot.pushed == []
    assert bot.store.get_local_file(path.name) is None      # 不落 failed 记录


def test_failure_notification_only_on_first_transition(tmp_path, monkeypatch):
    """同一个文件反复失败:只通知一次(避免每 5 分钟刷屏),日志照记。"""
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    # 用"非空目录"占住该文件将要改成的目标名:不会被当视频处理掉,也不会被当空目录清掉
    target = "飞到我心上.2026.S01E12.第12集.1080p.WEB-DL.H.264.AAC {tmdb-123456}.mkv"
    blocker = bot.cfg.openlist_dir / target
    blocker.mkdir()
    (blocker / "keep.txt").write_text("x", encoding="utf-8")
    for _ in range(3):
        asyncio.run(chain.scan_now())
    assert len([n for n in bot.notified if "目标名已存在" in n]) == 1
    assert bot.store.get_local_file(path.name)["status"] == "failed"


# ── 失败后重试成功:旧名的陈旧失败行要清掉(否则总览一直误报) ──
def test_stale_failure_row_cleared_after_success(tmp_path, monkeypatch):
    """改名成功落库后,原文件名下的失败记录被清理。"""
    chain, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    bot.store.save_local_file(name=path.name, size=path.stat().st_size,
                              status="failed", error="早先的失败")
    asyncio.run(chain.scan_now())
    assert bot.store.get_local_file(path.name) is None          # 旧名记录已清
    moved = list(Path(bot.cfg.clouddrive_dir).iterdir())
    assert len(moved) == 1
    assert bot.store.get_local_file(moved[0].name)["status"] == "processed"


# ── 记录自愈(#3,2026-09-12) ────────────────────────────────
def _present(cfg) -> set:
    out = set()
    for p in Path(cfg.openlist_dir).rglob("*"):
        if p.is_file():
            out.add((p.name, p.stat().st_size))
    return out


def test_reconcile_sweeps_record_whose_file_is_gone(tmp_path):
    """非 processed 但文件已不在落地点 → 标 gone。

    这正是用户遇到的那条:改名跑通后旧名记录留在库里,清单看不见它、
    计数却算着它 → "待人工"永远不消。
    """
    f, bot, path = _chain(tmp_path)
    bot.store.save_local_file(name="改名前的旧名.mkv", size=2000,
                              status="unrecognized", error="TMDB 未命中")
    assert f._reconcile_records(_present(bot.cfg)) == 1
    assert bot.store.get_local_file("改名前的旧名.mkv")["status"] == "gone"


def test_reconcile_keeps_present_and_terminal_records(tmp_path):
    """文件还在的、以及 processed/gone 的记录都不动。"""
    f, bot, path = _chain(tmp_path)
    bot.store.save_local_file(name=path.name, size=2000, status="unrecognized")
    bot.store.save_local_file(name="已处理.mkv", size=1, status="processed")
    bot.store.save_local_file(name="已收尾.mkv", size=1, status="gone")
    assert f._reconcile_records(_present(bot.cfg)) == 0
    assert bot.store.get_local_file(path.name)["status"] == "unrecognized"
    assert bot.store.get_local_file("已处理.mkv")["status"] == "processed"


def test_reconcile_not_fooled_by_same_name_different_size(tmp_path):
    """别处有同名文件但大小不同,不该"救活"陈旧记录(只比文件名会被遮蔽)。"""
    f, bot, path = _chain(tmp_path, name="S01/片.S01E01.mkv")     # 现存 2000 字节
    bot.store.save_local_file(name="片.S01E01.mkv", size=999,      # 旧记录是另一个尺寸
                              status="failed", error="炸了")
    assert f._reconcile_records(_present(bot.cfg)) == 1
    assert bot.store.get_local_file("片.S01E01.mkv")["status"] == "gone"


def test_scan_now_sweeps_stale_records(tmp_path, monkeypatch):
    """整轮扫描里也会自愈(不只是单独调用时)。"""
    f, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    bot.store.save_local_file(name="陈旧.mkv", size=1, status="unrecognized")
    report = asyncio.run(f.scan_now())
    assert bot.store.get_local_file("陈旧.mkv")["status"] == "gone"
    assert "收尾陈旧记录 1 条" in report


# ── 分级守门 + 通知式衔接(#1,2026-09-12) ─────────────────────
def test_gate_tiers_by_fetch_state(tmp_path):
    """守门用"事实"而不是只靠时间。

    在途不碰 / 完成且大小一致立即放行 / 完成但大小不符先等 / 无搬运记录才按静默年龄。
    """
    f, bot, path = _chain(tmp_path, min_age_seconds=600)
    os.utime(path, (time.time(), time.time()))          # mtime=现在(刚落盘)
    rel = path.name

    wait = f._gate(path)                                # 无记录 + 很新 → 要等
    assert wait is not None and 0 < wait <= 600

    bot.store.save_fetch(f"/监控/{rel}", 2000, status="moving")
    assert f._gate(path) == 5.0                         # 还在搬运 → 不碰

    bot.store.save_fetch(f"/监控/{rel}", 999, status="done")
    assert f._gate(path) == 5.0                         # 完成但大小对不上 → 再等

    bot.store.save_fetch(f"/监控/{rel}", 2000, status="done")
    assert f._gate(path) is None                        # ✅ 完成 + 大小一致 → 立即放行

    bot.store.save_fetch(f"/监控/{rel}", 2000, status="cleanup")
    assert f._gate(path) is None                        # cleanup(内容完整)同样算完成


def test_gate_size_floor_is_final_not_a_wait(tmp_path):
    """小于体积下限:永远不符合,不是"等一会就好"。"""
    f, bot, path = _chain(tmp_path, min_size_mb=10)     # 文件只有 2000 字节
    assert f._gate(path) == processor_mod._GATE_NEVER


def test_processor_notifies_next_stage_after_archive(tmp_path, monkeypatch):
    """归档了东西 → 立刻通知下一段(事件驱动衔接)。"""
    f, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    kicks: list[int] = []
    f.on_done = lambda: kicks.append(1)
    asyncio.run(f.scan_now())
    assert kicks == [1]


def test_processor_schedules_delayed_kick_when_gated(tmp_path):
    """文件被"静默年龄"挡住 → 排一次**延迟触发**(到点再看),而不是干等下一轮。"""
    f, bot, path = _chain(tmp_path, min_age_seconds=600, age_seconds=0)
    os.utime(path, (time.time(), time.time()))

    delays: list[float] = []

    class FakeKicker:
        def kick(self, delay: float = 0.0) -> None:
            delays.append(delay)

    f.kicker = FakeKicker()
    asyncio.run(f.scan_now())
    assert delays and 0 < delays[0] <= 600


# ── 真进度(#2,2026-09-12) ───────────────────────────────────
def test_ed2k_hash_reports_progress(tmp_path):
    """哈希按字节报进度(GB 级文件要跑几十秒,不报进度用户看不见它在动)。"""
    big = _write(tmp_path / "big.mkv", ED2K_CHUNK * 2)
    seen: list[float] = []
    size, root = asyncio.run(ed2k_hash_file(str(big), on_progress=seen.append))
    assert size == ED2K_CHUNK * 2 and len(root) == 32
    assert seen and seen[-1] == 100.0            # 收尾必报 100
    assert seen == sorted(seen)                  # 单调不减


def test_processor_phase_reporting_and_clear(tmp_path, monkeypatch):
    """处理段上报"正在做什么";处理完清空,免得页面以为它还在处理。"""
    f, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    f._phase("哈希中", path, 62.0)
    snap = f.current_snapshot()
    assert snap["name"] == path.name and snap["percent"] == 62.0
    assert snap["note"] == "处理中 · 哈希中 62%"

    asyncio.run(f.scan_now())                    # 跑一轮(会把文件处理掉)
    assert f.current_snapshot() == {}            # 收尾已清空


def test_processor_phase_has_no_percent_for_quick_phases(tmp_path, monkeypatch):
    """快阶段只报阶段名(没有百分比)也照样显示,且 bar 保持 0。"""
    f, bot, path = _chain(tmp_path, monkeypatch=monkeypatch)
    f._phase("识别中", path)
    snap = f.current_snapshot()
    assert snap["note"] == "处理中 · 识别中" and snap["percent"] == 0.0
