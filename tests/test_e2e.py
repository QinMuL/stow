"""全链路 e2e:获取段 → 处理段 → 上传段 一条龙(2026-09-14)。

用替身 openlist / CD2 / TMDB / ffprobe,不触网、不碰真实网盘。
验证:监控目录新资源 → 落到本地 → 改名/推卡(视频 + 伴行字幕 ed2k 同一张卡) →
归档 clouddrive → 上传到 115(CD2 移动语义)→ 本地源清空。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Config
from app.fetcher import ResourceFetcher
from app.processor import ProcessChain
from app.store import Store
from app.uploader import Uploader

_MON = "/夸克云盘/stow监控"
_DEST = "/项目测试"
_CD2_SRC = "/clouddrive"
_CD2_DEST = "/115open/影库"


# ── 替身外部服务 ────────────────────────────────────────────
class FakeOpenList:
    """替身 openlist:监控目录有资源;move 提交任务;任务状态可编排。"""

    def __init__(self) -> None:
        self.items: dict[str, list[dict]] = {}
        self.undone: list[dict] = []
        self.done: list[dict] = []
        self.moves: list[tuple[str, str, list[str]]] = []
        self._seq = 0

    async def list_dir(self, path, per_page=200):
        return self.items.get(path, [])

    async def move(self, src_dir, dst_dir, names):
        self.moves.append((src_dir, dst_dir, list(names)))
        self._seq += 1
        t = {"id": f"T{self._seq}", "name": f"move {names[0]}", "state": 1, "progress": 0}
        self.undone.append(t)
        return [t]

    async def remove(self, dir_path, names):
        self.items[dir_path] = [i for i in self.items.get(dir_path, [])
                                if i["name"] not in set(names)]

    async def move_tasks(self, *, undone):
        return self.undone if undone else self.done

    async def aclose(self):
        return None


class FakeCd2:
    """替身 CD2:记录 move;任务列表可编排。"""

    def __init__(self) -> None:
        self.moves: list[tuple[list[str], str]] = []
        self.tasks: list[dict] = []
        self.deleted: list[str] = []
        self.dirs: dict[str, list[str]] = {}

    def system_info(self):
        return {"logged_in": True, "user_name": "t", "ready": True, "message": ""}

    def sub_files(self, path, **kw):
        class F:
            def __init__(self, n):
                self.name, self.is_dir, self.size = n, True, 0
        return [F(n) for n in self.dirs.get(path, [])]

    def create_folder(self, parent, name):
        self.dirs.setdefault(parent, []).append(name)
        return True

    def move_file(self, src_paths, dest_path):
        self.moves.append((list(src_paths), dest_path))
        return {"success": True, "error": "", "raw": ""}

    def copy_tasks(self):
        return list(self.tasks)

    def delete_file(self, path):
        self.deleted.append(path)
        return True

    def close(self):
        pass


class FakeTmdb:
    def __init__(self, details):
        self.details = details

    async def match(self, media):
        return self.details


class FakeProbe:
    async def __call__(self, path, **kw):
        from app.probe import ProbeTags

        return ProbeTags(resolution="2160p", video_codec="H.265", audio_codec="DDP 5.1",
                         video_tracks=1, audio_tracks=1, duration=100.0)


class E2eBot:
    """三段共用的假 bot:cfg / store / 通知 / tmdb / push_link。"""

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.pushed: list = []
        self.notified: list[str] = []
        self.push_kw: dict | None = None
        self.tmdb = FakeTmdb({"title": "飞到我心上", "year": 2026, "tmdb_id": 123456})

    async def push_link(self, link, **kw):
        from app.bot import PushResult

        self.pushed.append(link)
        self.push_kw = kw
        return PushResult(True, "✅ 已推送")

    async def _notify_uid(self, uid, text):
        self.notified.append(text)


def _full_chain(tmp_path, monkeypatch):
    """搭好三段(与 bot.run 同构的链式 on_done),返回 (cfg, bot, fetcher, chain, uploader, ol, cd2)。"""
    from app import processor as processor_mod

    cfg = Config(
        data_dir=str(tmp_path), media_root=str(tmp_path / "media"),
        openlist_base_url="http://127.0.0.1:5244", openlist_token="tok",
        openlist_monitor_dirs=_MON, openlist_dest_path=_DEST,
        openlist_max_tasks=4,
        min_size_mb=0, min_age_seconds=0, clean_enabled=False,
        cd2_address="127.0.0.1:19798", cd2_token="tok",
        cd2_source_path=_CD2_SRC, cd2_dest_path=_CD2_DEST,
        upload_max_tasks=4, tg_admin_ids=[1],
    )
    cfg.ensure_media_dirs()
    bot = E2eBot(cfg, Store(tmp_path / "t.db"))
    monkeypatch.setattr(processor_mod, "probe_file", FakeProbe())

    fetcher = ResourceFetcher(bot)
    chain = ProcessChain(bot)
    uploader = Uploader(bot)
    fetcher.on_done = lambda: chain.kicker.kick()      # 搬完 → 处理段看一轮
    chain.on_done = lambda: uploader.kicker.kick()     # 归档完 → 上传段看一轮

    ol = FakeOpenList()
    ol.items[_MON] = [
        {"name": "飞到我心上.2026.WEB-DL.S01E12.mkv", "size": 2000, "is_dir": False},
        {"name": "飞到我心上.2026.WEB-DL.S01E12.zh.srt", "size": 50, "is_dir": False},
        {"name": "飞到我心上.2026.WEB-DL.S01E12.sup", "size": 100, "is_dir": False},
    ]
    fetcher.client = ol
    cd2 = FakeCd2()
    uploader.client = cd2
    return cfg, bot, fetcher, chain, uploader, ol, cd2


def test_full_pipeline_fetch_process_upload(tmp_path, monkeypatch):
    """整条链:资源落地点出现 → 三段自动流转 → 115 入库、本地清空、卡片带字幕。"""
    cfg, bot, fetcher, chain, uploader, ol, cd2 = _full_chain(tmp_path, monkeypatch)
    landing = Path(cfg.openlist_dir)
    cloud = Path(cfg.clouddrive_dir)

    # ── ① 获取段:发现新资源并提交搬运 ──
    report = asyncio.run(fetcher.scan_now())
    assert "提交 3 个" in report
    assert len(ol.undone) == 3 and len(bot.store.list_fetch("moving")) == 3

    # ── ② 模拟 openlist 移动完成:任务转 done,文件落到本地落地点 ──
    (landing / "飞到我心上.2026.WEB-DL.S01E12.mkv").write_bytes(b"x" * 2000)
    (landing / "飞到我心上.2026.WEB-DL.S01E12.zh.srt").write_text("sub", encoding="utf-8")
    (landing / "飞到我心上.2026.WEB-DL.S01E12.sup").write_bytes(b"sup")
    ol.done = ol.undone[:]
    for t in ol.done:
        t["state"] = 2
    ol.undone = []

    report = asyncio.run(fetcher.scan_now())
    assert "完成 3 个" in report
    assert all(r["status"] == "done" for r in bot.store.list_fetch())

    # ── ③ 处理段:改名 → 推卡(视频 + 伴行字幕 ed2k 一张卡)→ 归档 ──
    report = asyncio.run(chain.scan_now())
    assert "processed 1" in report
    assert len(bot.pushed) == 1, f"应只推 1 张视频卡,实际 {len(bot.pushed)}"
    assert bot.pushed[0].url.startswith("ed2k://|file|飞到我心上.2026.S01E12")
    subs = bot.push_kw.get("extra_links") or []
    assert len(subs) == 2, f"应带 2 条字幕链接,实际 {len(subs)}"
    assert any(".zh.srt|" in u for u in subs) and any(".sup|" in u for u in subs)

    archived = {p.name for p in cloud.iterdir()}
    assert len(archived) == 3, f"归档应有视频+2 字幕,实际 {archived}"     # 视频 + srt + sup 伴行
    vid = next(n for n in archived if n.endswith(".mkv"))
    assert f"{vid[:-len('.mkv')]}.zh.srt" in archived
    assert f"{vid[:-len('.mkv')]}.sup" in archived
    assert next(landing.iterdir(), None) is None, "落地点应已清空(视频和字幕都已归档)"

    # ── ④ 上传段:clouddrive → 115(CD2 移动语义) ──
    report = asyncio.run(uploader.scan_now())
    assert "已提交 3 个" in report
    assert len(cd2.moves) == 3
    assert all(src[0][0].startswith(f"{_CD2_SRC}/") for src in cd2.moves)

    # 模拟 CD2 移动完成:任务消失 + 本地源被带走(移动语义的自然信号)
    for p in list(cloud.iterdir()):
        p.unlink()
    cd2.tasks = []
    settled = asyncio.run(uploader._poll_tasks())
    assert settled == {"done": 3, "failed": 0}
    assert len(bot.store.list_uploads("done")) == 3

    # ── ⑤ 终态:三段记录齐全、本地清空、无失败通知 ──
    assert all(r["status"] == "done" for r in bot.store.list_fetch())
    assert len(bot.store.list_local_files("processed")) == 1
    assert not list(cloud.iterdir())                     # 本地上传源已清空
    assert bot.notified == []                            # 全程无失败通知


def test_full_pipeline_process_blocks_unrecognized(tmp_path, monkeypatch):
    """负向:TMDB 未命中时,处理段拦下不推卡,资源留在落地点不上传。"""
    cfg, bot, fetcher, chain, uploader, ol, cd2 = _full_chain(tmp_path, monkeypatch)
    bot.tmdb = FakeTmdb(None)                            # 识别不出 → 处理段拦下
    landing = Path(cfg.openlist_dir)
    (landing / "飞到我心上.2026.WEB-DL.S01E12.mkv").write_bytes(b"x" * 2000)
    ol.done = ol.undone[:]
    for t in ol.done:
        t["state"] = 2
    ol.undone = []
    asyncio.run(fetcher.scan_now())                      # 结算搬完

    report = asyncio.run(chain.scan_now())
    assert "unrecognized 1" in report
    assert bot.pushed == []
    assert list(Path(cfg.clouddrive_dir).iterdir()) == []   # 不归档不上传
    assert (landing / "飞到我心上.2026.WEB-DL.S01E12.mkv").exists()
    assert bot.notified and "TMDB 未命中" in bot.notified[0]
