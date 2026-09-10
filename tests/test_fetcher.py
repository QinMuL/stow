"""获取段测试:发现新资源 → 提交 move(并发闸门)→ 按任务状态结算。

用假 openlist 客户端与假 bot,不触网、不碰真实网盘。
"""

from __future__ import annotations

import asyncio
import time

from app.config import Config
from app.fetcher import ResourceFetcher
from app.openlist import OpenListError
from app.store import Store

_MON = "/夸克云盘/001临时影库"
_DEST = "/项目测试"


class FakeClient:
    """替身 OpenList 客户端:记录 move 调用,任务状态可编排。"""

    def __init__(self) -> None:
        self.items: dict[str, list[dict]] = {}      # 目录 → 条目
        self.moves: list[tuple[str, str, list[str]]] = []
        self.undone: list[dict] = []
        self.done: list[dict] = []
        self.fail_move = False
        self._seq = 0

    async def list_dir(self, path, per_page=200):
        return self.items.get(path, [])

    async def move(self, src_dir, dst_dir, names):
        if self.fail_move:
            raise OpenListError("move 端点报错")
        self.moves.append((src_dir, dst_dir, list(names)))
        self._seq += 1
        task = {"id": f"T{self._seq}", "name": f"move {src_dir}/{names[0]}", "state": 1,
                "progress": 0}
        self.undone.append(task)
        return [task]

    async def move_tasks(self, *, undone):
        return self.undone if undone else self.done

    async def aclose(self):
        return None


class FakeBot:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.notified: list[str] = []

    async def _notify_uid(self, uid, text):
        self.notified.append(text)


def _fetcher(tmp_path, *, max_tasks=2, files=None, monitor=_MON):
    cfg = Config(
        data_dir=str(tmp_path), media_root=str(tmp_path / "media"),
        openlist_base_url="http://127.0.0.1:5244", openlist_token="tok",
        openlist_monitor_dirs=monitor, openlist_dest_path=_DEST,
        openlist_max_tasks=max_tasks, tg_admin_ids=[5406565010],
    )
    cfg.ensure_media_dirs()
    bot = FakeBot(cfg, Store(tmp_path / "t.db"))
    f = ResourceFetcher(bot)
    client = FakeClient()
    client.items[_MON] = files if files is not None else [
        {"name": "何以为家 (2018)", "size": 0, "is_dir": True},
        {"name": "茶啊二中.S06E01.mkv", "size": 1000, "is_dir": False},
    ]
    f.client = client
    return f, bot, client


# ── 启用判定 ────────────────────────────────────────────────
def test_disabled_without_monitor_dirs(tmp_path):
    f, bot, _ = _fetcher(tmp_path, monitor="")
    assert f.enabled() is False
    assert "未启用" in asyncio.run(f.scan_now())


def test_disabled_without_credentials(tmp_path):
    f, bot, _ = _fetcher(tmp_path)
    bot.cfg.openlist_token = ""
    f2 = ResourceFetcher(bot)
    assert f2.enabled() is False


# ── 发现与提交 ──────────────────────────────────────────────
def test_scan_submits_move_for_new_items(tmp_path):
    f, bot, client = _fetcher(tmp_path)
    report = asyncio.run(f.scan_now())
    assert len(client.moves) == 2                       # 目录项与文件项各提交一个
    assert client.moves[0] == (_MON, _DEST, ["何以为家 (2018)"])
    assert "提交 2 个" in report
    rows = bot.store.list_fetch("moving")
    assert {r["src_path"].rsplit("/", 1)[-1] for r in rows} == {"何以为家 (2018)", "茶啊二中.S06E01.mkv"}
    assert all(r["task_id"] for r in rows)              # 记下了任务 id


def test_second_scan_skips_inflight(tmp_path):
    f, bot, client = _fetcher(tmp_path)
    asyncio.run(f.scan_now())
    asyncio.run(f.scan_now())                           # 再扫:在途的不重复提交
    assert len(client.moves) == 2
    assert "跳过 2 个" in asyncio.run(f.scan_now())


def test_concurrency_gate_limits_to_max(tmp_path):
    """并发闸门:上限 1 时,一轮内只提交一个,其余留到下一轮。"""
    files = [{"name": f"剧集.E{i:02d}.mkv", "size": 500, "is_dir": False} for i in range(1, 4)]
    f, bot, client = _fetcher(tmp_path, max_tasks=1, files=files)
    asyncio.run(f.scan_now())
    assert len(client.moves) == 1                       # 只提交 1 个
    assert len(bot.store.list_fetch("moving")) == 1
    # 完成这个后再扫 → 提交下一个
    client.undone[0]["state"] = 2
    client.done.append(client.undone.pop(0))
    asyncio.run(f.scan_now())
    assert len(client.moves) == 2


def test_skips_item_already_local_same_size(tmp_path):
    """本地已有同名同大小(人工搬过/重启前搬完)→ 直接记完成,不重复搬。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "已搬.mkv", "size": 7, "is_dir": False}])
    (bot.cfg.openlist_dir / "已搬.mkv").write_bytes(b"1234567")
    report = asyncio.run(f.scan_now())
    assert client.moves == []
    assert bot.store.get_fetch(f"{_MON}/已搬.mkv")["status"] == "done"
    assert "跳过 1 个" in report


def test_move_error_does_not_mark_moving(tmp_path):
    f, bot, client = _fetcher(tmp_path, files=[{"name": "x.mkv", "size": 1, "is_dir": False}])
    client.fail_move = True
    asyncio.run(f.scan_now())
    assert bot.store.list_fetch("moving") == []          # 提交失败不记在途
    assert bot.store.get_fetch(f"{_MON}/x.mkv") is None


# ── 任务结算 ────────────────────────────────────────────────
def test_task_success_marks_done(tmp_path):
    f, bot, client = _fetcher(tmp_path, files=[{"name": "a.mkv", "size": 10, "is_dir": False}])
    asyncio.run(f.scan_now())
    client.undone[0]["state"] = 2
    client.done.append(client.undone.pop(0))
    report = asyncio.run(f.scan_now())
    assert bot.store.get_fetch(f"{_MON}/a.mkv")["status"] == "done"
    assert bot.store.fetch_stats() == {"done": 1}
    assert "完成 1 个" in report


def test_task_failure_marks_failed_and_notifies(tmp_path):
    f, bot, client = _fetcher(tmp_path, files=[{"name": "b.mkv", "size": 10, "is_dir": False}])
    asyncio.run(f.scan_now())
    client.undone[0].update({"state": 5, "error": "网络中断"})
    client.done.append(client.undone.pop(0))
    asyncio.run(f.scan_now())
    row = bot.store.get_fetch(f"{_MON}/b.mkv")
    assert row["status"] == "failed" and row["attempts"] == 1 and "网络中断" in row["error"]
    assert bot.notified and "获取失败" in bot.notified[0]


def _age_backoff(bot, src: str) -> None:
    """把 updated_at 拨到退避期之前,模拟"等够了再重试"。"""
    bot.store._conn.execute("UPDATE fetch_state SET updated_at=? WHERE src_path=?",
                            (time.time() - 3600, src))
    bot.store._conn.commit()


def test_failed_item_retries_after_backoff_until_max(tmp_path):
    """失败后退避重试,最多 MAX_ATTEMPTS 次;退避期内不重试。"""
    src = f"{_MON}/c.mkv"
    f, bot, client = _fetcher(tmp_path, files=[{"name": "c.mkv", "size": 10, "is_dir": False}])
    for i in range(3):
        asyncio.run(f.scan_now())                        # 提交
        assert len(client.moves) == i + 1
        client.undone[0].update({"state": 5, "error": f"失败{i}"})
        client.done.append(client.undone.pop(0))
        asyncio.run(f.scan_now())                        # 结算失败
        asyncio.run(f.scan_now())                        # 退避期内:不重试
        assert len(client.moves) == i + 1
        _age_backoff(bot, src)
    assert bot.store.get_fetch(src)["attempts"] == 3
    asyncio.run(f.scan_now())                            # 超过上限:不再提交
    assert len(client.moves) == 3


def test_task_lost_treated_failed_after_timeout(tmp_path):
    f, bot, client = _fetcher(tmp_path, files=[{"name": "d.mkv", "size": 10, "is_dir": False}])
    asyncio.run(f.scan_now())
    client.undone.clear()                                # 任务凭空消失且目标不在本地
    asyncio.run(f.scan_now())
    assert bot.store.get_fetch(f"{_MON}/d.mkv")["status"] == "moving"   # 未到阈值:继续等
    _age_backoff(bot, f"{_MON}/d.mkv")                 # 把在途时间拨老,模拟任务长期无踪影
    asyncio.run(f.scan_now())
    assert bot.store.get_fetch(f"{_MON}/d.mkv")["status"] == "failed"


def test_landed_file_counts_as_done_without_task(tmp_path):
    """任务秒完成且不留痕:目标文件已落地且大小相符 → 记完成。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "e.mkv", "size": 4, "is_dir": False}])
    asyncio.run(f.scan_now())
    client.undone.clear()
    client.done.clear()
    (bot.cfg.openlist_dir / "e.mkv").write_bytes(b"data")
    asyncio.run(f.scan_now())
    assert bot.store.get_fetch(f"{_MON}/e.mkv")["status"] == "done"


def test_restart_keeps_tracking_task(tmp_path):
    """重启安全:库里的在途任务由新实例继续跟踪。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "f.mkv", "size": 10, "is_dir": False}])
    asyncio.run(f.scan_now())
    tid = bot.store.list_fetch("moving")[0]["task_id"]
    # 模拟重启:新实例复用同一个 store 与客户端
    f2 = ResourceFetcher(bot)
    f2.client = client
    assert f2._active_count() == 1
    client.undone[0].update({"state": 2})
    client.done.append(client.undone.pop(0))
    asyncio.run(f2.scan_now())
    row = bot.store.get_fetch(f"{_MON}/f.mkv")
    assert row["status"] == "done" and row["task_id"] == tid   # 任务 id 一路保留
