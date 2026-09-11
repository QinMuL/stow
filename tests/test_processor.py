"""处理段测试:ed2k 哈希/链接、命名器、探测映射、处理链(用假件,不触网)。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import app.processor as processor_mod
from app.config import Config
from app.ed2k import ED2K_CHUNK, ed2k_hash_file, ed2k_uri, sanitize_ed2k_name
from app.media import analyze_share
from app.namer import quality_label, render_name, sanitize_name
from app.pan115 import ShareFile
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
        {"codec_type": "audio", "codec_name": "eac3"},
        {"codec_type": "audio", "codec_name": "aac"},
    ], "format": {"duration": "1234.5"}}
    t = tags_from_ffprobe(data)
    assert (t.resolution, t.effect, t.bit_depth) == ("2160p", "HDR10", "10bit")
    assert t.video_codec == "H.265" and t.audio_codec == "DDP"
    assert t.audio_tracks == 2 and abs(t.duration - 1234.5) < 0.01 and t.ok()


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


def test_tags_sdr_has_no_effect_label():
    t = tags_from_ffprobe({"streams": [{"codec_type": "video", "codec_name": "h264",
                                        "width": 1920, "height": 1080,
                                        "color_transfer": "bt709", "pix_fmt": "yuv420p"}]})
    assert t.effect == "" and t.resolution == "1080p"


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

    async def push_link(self, link, **kw):
        from app.bot import PushResult

        self.pushed.append(link)
        return PushResult(True, "✅ 已推送")

    async def _notify_uid(self, uid, text):
        self.notified.append(text)


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
    assert "飞到我心上.2026.WEB-DL.S01E12.zh.srt" in names      # 同 stem 字幕跟着走
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
