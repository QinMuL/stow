"""获取段测试:发现新资源 → 提交 move(并发闸门)→ 按任务状态结算。

用假 openlist 客户端与假 bot,不触网、不碰真实网盘。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

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

    async def remove(self, dir_path, names):
        # 基类替身默认"删源成功":从 items 里摘掉(与真实 openlist 一致)
        self.items[dir_path] = [i for i in self.items.get(dir_path, [])
                                if i["name"] not in set(names)]

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
    """目录被**展开成逐文件**提交(每个文件一个任务),相对子目录结构保留。"""
    f, bot, client = _fetcher(tmp_path, max_tasks=3)     # 放高上限,一轮内全部提交
    client.items[f"{_MON}/何以为家 (2018)"] = [
        {"name": "何以为家.2018.1080p.mkv", "size": 100, "is_dir": False},
        {"name": "字幕", "size": 0, "is_dir": True},
    ]
    client.items[f"{_MON}/何以为家 (2018)/字幕"] = [
        {"name": "zh.srt", "size": 10, "is_dir": False},
    ]
    report = asyncio.run(f.scan_now())
    assert len(client.moves) == 3                       # 顶层 1 + 目录内 2 = 3 个任务
    assert (_MON, _DEST, ["茶啊二中.S06E01.mkv"]) in client.moves
    assert (_MON + "/何以为家 (2018)", f"{_DEST}/何以为家 (2018)",
            ["何以为家.2018.1080p.mkv"]) in client.moves
    assert (_MON + "/何以为家 (2018)/字幕", f"{_DEST}/何以为家 (2018)/字幕",
            ["zh.srt"]) in client.moves                  # 嵌套子目录也保留
    assert "提交 3 个" in report
    rows = bot.store.list_fetch("moving")
    assert len(rows) == 3 and all(r["task_id"] for r in rows)


def test_second_scan_skips_inflight(tmp_path):
    f, bot, client = _fetcher(tmp_path)
    client.items[f"{_MON}/何以为家 (2018)"] = [
        {"name": "a.mkv", "size": 100, "is_dir": False}]
    asyncio.run(f.scan_now())
    n = len(client.moves)
    asyncio.run(f.scan_now())                           # 再扫:在途的不重复提交
    assert len(client.moves) == n
    assert "跳过" in asyncio.run(f.scan_now())


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


def test_poll_settles_without_scanning(tmp_path):
    """结算与扫描解耦:不跑 scan_now 也能把在途任务结掉(否则要等下一轮扫描)。

    旧实现里 _poll_tasks 只在大轮扫描内执行,4.5GB 搬完仍要占 5 分钟并发位。
    """
    f, bot, client = _fetcher(tmp_path, files=[{"name": "g.mkv", "size": 10, "is_dir": False}])
    asyncio.run(f.scan_now())                     # 提交,进入 moving
    client.undone[0].update({"state": 2})         # 任务完成
    client.done.append(client.undone.pop(0))

    settled = asyncio.run(f._poll_tasks())        # 只结算,不扫描
    assert settled == {"done": 1, "failed": 0}
    assert bot.store.get_fetch(f"{_MON}/g.mkv")["status"] == "done"
    assert f._active_count() == 0                 # 并发位立刻腾出


def test_poll_is_coroutine_safe(tmp_path):
    """扫描与轮询两条路都会结算:并发调用不重复计数(加锁串行)。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "h.mkv", "size": 10, "is_dir": False}])
    asyncio.run(f.scan_now())
    client.undone[0].update({"state": 2})
    client.done.append(client.undone.pop(0))

    async def two():
        return await asyncio.gather(f._poll_tasks(), f._poll_tasks())

    a, b = asyncio.run(two())
    assert a["done"] + b["done"] == 1             # 只结算一次


# ── 上传段(CD2 移动语义) ────────────────────────────────────
class FakeCd2:
    """替身 CD2:记录 move 调用,任务列表可编排。"""

    def __init__(self) -> None:
        self.moves: list[tuple[list[str], str]] = []
        self.tasks: list[dict] = []
        self.dirs: dict[str, list[str]] = {}
        self.created: list[tuple[str, str]] = []
        self.move_ok = True
        self.deleted: list[str] = []

    def system_info(self):
        return {"logged_in": True, "user_name": "test", "ready": True, "message": ""}

    def sub_files(self, path, **kw):
        class F:
            def __init__(self, n): self.name, self.is_dir, self.size = n, True, 0
        return [F(n) for n in self.dirs.get(path, [])]

    def create_folder(self, parent, name):
        self.created.append((parent, name))
        return True

    def move_file(self, src_paths, dest_path):
        if not self.move_ok:
            return {"success": False, "error": "目标不可写", "raw": ""}
        self.moves.append((list(src_paths), dest_path))
        return {"success": True, "error": "", "raw": ""}

    def copy_tasks(self):
        return list(self.tasks)

    def delete_file(self, path):
        self.deleted.append(path)
        return True

    def close(self):
        pass


def _uploader(tmp_path, *, files=("a.mkv",), dest="/115open/目标目录", source="/clouddrive"):
    from app.config import Config as _C
    from app.uploader import Uploader

    cfg = _C(data_dir=str(tmp_path), media_root=str(tmp_path / "media"),
             cd2_address="127.0.0.1:19798", cd2_token="tok",
             cd2_source_path=source, cd2_dest_path=dest, tg_admin_ids=[1],
             min_size_mb=0, min_age_seconds=0)
    cfg.ensure_media_dirs()
    bot = FakeBot(cfg, Store(tmp_path / "t.db"))
    up = Uploader(bot)
    up.client = FakeCd2()
    for name in files:
        (Path(cfg.clouddrive_dir) / name).write_bytes(b"x" * 100)
    return up, bot, up.client


def test_upload_disabled_without_dest(tmp_path):
    up, bot, _ = _uploader(tmp_path, dest="")
    assert up.enabled() is False
    assert "未启用上传段" in asyncio.run(up.scan_now())


def test_upload_uses_move_and_records_state(tmp_path):
    """上传走 MoveFile(不是复制);提交后本地源在,完成由"源消失"确认。"""
    up, bot, client = _uploader(tmp_path, files=("剧名.S01E01.mkv",))
    report = asyncio.run(up.scan_now())
    assert "已提交" in report
    assert client.moves == [(["/clouddrive/剧名.S01E01.mkv"], "/115open/目标目录")]
    row = bot.store.get_upload("剧名.S01E01.mkv")
    assert row["status"] == "uploading" and row["dest"] == "/115open/目标目录"


def test_upload_concurrency_limit(tmp_path):
    """并发上限:默认 2 —— 一轮内提交两个,第三个等在途腾位(用户 2026-09-11 指出 CD2 可同时跑两个)。"""
    up, bot, client = _uploader(tmp_path, files=("a.mkv", "b.mkv", "c.mkv"))
    asyncio.run(up.scan_now())
    assert len(client.moves) == 2                      # 并发 2
    asyncio.run(up.scan_now())                          # 已有 2 个在途 → 不再提交
    assert len(client.moves) == 2
    assert "在途 2 个" in asyncio.run(up.scan_now())


def test_upload_concurrency_configurable(tmp_path):
    """并发上限可配:设为 1 即回到串行。"""
    up, bot, client = _uploader(tmp_path, files=("a.mkv", "b.mkv"))
    bot.cfg.upload_max_tasks = 1
    asyncio.run(up.scan_now())
    assert len(client.moves) == 1


def test_upload_completes_when_source_gone(tmp_path):
    """移动完成 = CD2 里没有该任务 且 本地源已消失 → 记 done。"""
    up, bot, client = _uploader(tmp_path, files=("c.mkv",))
    asyncio.run(up.scan_now())
    (bot.cfg.clouddrive_dir / "c.mkv").unlink()        # 移动语义:源被带走
    client.tasks = []
    settled = asyncio.run(up._poll_tasks())
    assert settled == {"done": 1, "failed": 0}
    assert bot.store.get_upload("c.mkv")["status"] == "done"


def test_upload_deletes_source_after_done(tmp_path):
    """CD2 跨云 MoveFile 实测不删源 → 完成后补删本地源(幂等)。"""
    up, bot, client = _uploader(tmp_path, files=("h.mkv",))
    asyncio.run(up.scan_now())
    client.tasks = [{"mode": 0, "status_raw": 3, "source": "/clouddrive/h.mkv",
                     "dest": "/115open/目标目录", "total_bytes": 100, "uploaded_bytes": 100,
                     "progress": 100.0, "files": 1, "uploaded_files": 1, "error": ""}]
    assert asyncio.run(up._poll_tasks()) == {"done": 1, "failed": 0}
    assert client.deleted == ["/clouddrive/h.mkv"]        # 补删源
    assert bot.store.get_upload("h.mkv")["status"] == "done"


def test_upload_completion_by_content_not_enum(tmp_path):
    """完成判定只看内容(bytes 到齐 + 无 error),不认枚举名(版本口径会变)。"""
    up, bot, client = _uploader(tmp_path, files=("d.mkv",))
    asyncio.run(up.scan_now())
    client.tasks = [{"mode": 1, "status_raw": 3, "source": "/clouddrive/d.mkv",
                     "dest": "/115open/目标目录", "total_bytes": 100, "uploaded_bytes": 100,
                     "progress": 100.0, "files": 1, "uploaded_files": 1, "error": ""}]
    assert asyncio.run(up._poll_tasks()) == {"done": 1, "failed": 0}


def test_upload_failure_notifies_and_keeps_local(tmp_path):
    up, bot, client = _uploader(tmp_path, files=("e.mkv",))
    asyncio.run(up.scan_now())
    client.tasks = [{"mode": 1, "status_raw": 4, "source": "/clouddrive/e.mkv",
                     "dest": "/115open/目标目录", "total_bytes": 100, "uploaded_bytes": 30,
                     "progress": 30.0, "files": 1, "uploaded_files": 0, "error": "网络中断"}]
    settled = asyncio.run(up._poll_tasks())
    assert settled == {"done": 0, "failed": 1}
    assert (bot.cfg.clouddrive_dir / "e.mkv").exists()      # 本地仍在
    assert bot.notified and "上传失败" in bot.notified[0]


def test_upload_creates_missing_dest_dir(tmp_path):
    up, bot, client = _uploader(tmp_path, files=("f.mkv",), dest="/115open/新目标")
    client.dirs = {"/115open": []}                          # 目标不存在
    asyncio.run(up.scan_now())
    assert client.created == [("/115open", "新目标")]


def test_upload_resumes_after_restart(tmp_path):
    """重启安全:库里的在途任务由新实例继续结算,不重复提交。"""
    from app.uploader import Uploader

    up, bot, client = _uploader(tmp_path, files=("g.mkv",))
    asyncio.run(up.scan_now())
    up2 = Uploader(bot)
    up2.client = client
    asyncio.run(up2.scan_now())                             # 重启后一轮
    assert len(client.moves) == 1                            # 不重复提交
    (bot.cfg.clouddrive_dir / "g.mkv").unlink()
    client.tasks = []
    assert asyncio.run(up2._poll_tasks()) == {"done": 1, "failed": 0}


# ── 移动"成功但没删源"的自愈(实测 openlist 偶发) ─────────────
class FakeClientWithRemain(FakeClient):
    """模拟 openlist 报成功但源仍在(不自动摘 items),记录 remove 调用。"""

    def __init__(self) -> None:
        super().__init__()
        self.removed: list[tuple[str, list[str]]] = []
        self.remove_fails = False

    async def remove(self, dir_path, names):
        if self.remove_fails:
            raise OpenListError("夸克删除失败:权限不足")
        self.removed.append((dir_path, list(names)))


def _fetcher_with_remain(tmp_path):
    f, bot, _ = _fetcher(tmp_path, files=[{"name": "广域.E18.mkv", "size": 10, "is_dir": False}])
    client = FakeClientWithRemain()
    client.items[_MON] = [{"name": "广域.E18.mkv", "size": 10, "is_dir": False}]
    f.client = client
    return f, bot, client


def test_stray_source_is_deleted_after_done(tmp_path):
    """openlist 报移动成功但源仍在 → 补删 + 告警(不重复提交)。"""
    f, bot, client = _fetcher_with_remain(tmp_path)
    asyncio.run(f.scan_now())                    # 提交
    client.undone[0].update({"state": 2})        # 任务成功
    client.done.append(client.undone.pop(0))
    asyncio.run(f.scan_now())                    # 结算 → 校验源 → 补删
    assert client.removed == [(_MON, ["广域.E18.mkv"])]
    assert bot.store.get_fetch(f"{_MON}/广域.E18.mkv")["status"] == "done"
    assert len(client.moves) == 1                 # 不会因为源还在而重复提交


def test_stray_source_delete_failure_defers_to_retry(tmp_path):
    """补删也失败 → **不当场打扰人**(2026-09-12 改):记成待清理,交给后续轮次重试;
    重试到头才通知。实测这种偶发不罕见(6 集里 4 集命中),一次不成推给人代价太大。"""
    f, bot, client = _fetcher_with_remain(tmp_path)
    asyncio.run(f.scan_now())
    client.undone[0].update({"state": 2})
    client.done.append(client.undone.pop(0))
    client.remove_fails = True
    asyncio.run(f.scan_now())

    assert bot.notified == []                       # 第一次失败不惊动人
    rows = bot.store.list_fetch("cleanup")
    assert len(rows) == 1 and rows[0]["attempts"] == 1
    # 且不会因为状态变了就重复提交(源文件还在监控目录里)
    assert len(client.moves) == 1


def test_settle_frees_slot_and_chains_next_submit(tmp_path):
    """并发位腾出后立刻补提交(不等下一轮扫描)。"""
    files = [{"name": f"剧.E{i:02d}.mkv", "size": 100, "is_dir": False} for i in (1, 2, 3)]
    f, bot, client = _fetcher(tmp_path, max_tasks=1, files=files)
    asyncio.run(f.scan_now())
    assert len(client.moves) == 1
    # 完成任务后走 poll_loop 的那条路径:结算 → 立刻补提交
    client.undone[0].update({"state": 2})
    client.done.append(client.undone.pop(0))
    settled = asyncio.run(f._poll_tasks())
    assert settled["done"] == 1
    asyncio.run(f._scan_dirs())
    assert len(client.moves) == 2


def test_upload_settle_chains_next_submit(tmp_path):
    """上传段:腾出位后立刻补提交(实测单个仅 ~19s,等轮会白等 5 分钟)。"""
    up, bot, client = _uploader(tmp_path, files=("a.mkv", "b.mkv", "c.mkv"))
    bot.cfg.upload_max_tasks = 1                     # 用串行凸显"腾位即补"
    asyncio.run(up.scan_now())
    assert len(client.moves) == 1
    (bot.cfg.clouddrive_dir / "a.mkv").unlink()      # 第一个传完(源消失)
    client.tasks = []
    settled = asyncio.run(up._poll_tasks())
    assert settled["done"] == 1
    assert len(asyncio.run(up._submit_ready())) == 1  # poll_loop 里就跟着做这一步
    assert len(client.moves) == 2


# ── 目录类记录:不做 size 比对(实测 drvfs 512 / ext4 4096 / openlist 报 0) ──
def test_folder_landed_by_existence_not_size(tmp_path):
    f, bot, _ = _fetcher(tmp_path, files=[{"name": "某季", "size": 0, "is_dir": True}])
    row = {"src_path": f"{_MON}/某季", "src_size": 0, "name": "某季"}
    assert f._landed(row) is False                       # 还没落地
    d = bot.cfg.openlist_dir / "某季"
    d.mkdir(parents=True)
    assert f._landed(row) is True                        # 目录存在即算落地(512≠0 也认)


def test_folder_files_already_local_are_skipped(tmp_path):
    """目录内文件已本地存在(同大小)→ 跳过该文件;其余文件照常提交。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "某季", "size": 0, "is_dir": True}])
    client.items[f"{_MON}/某季"] = [
        {"name": "已搬.mkv", "size": 7, "is_dir": False},
        {"name": "新片.mkv", "size": 9, "is_dir": False},
    ]
    (bot.cfg.openlist_dir / "某季").mkdir(parents=True)
    (bot.cfg.openlist_dir / "某季" / "已搬.mkv").write_bytes(b"1234567")
    asyncio.run(f.scan_now())
    assert [m[2] for m in client.moves] == [["新片.mkv"]]          # 只提交缺的那个
    assert bot.store.get_fetch(f"{_MON}/某季/已搬.mkv")["status"] == "done"


def test_folder_submits_one_by_one_respecting_limit(tmp_path):
    """并发粒度=文件:上限 2 时,含 3 文件的目录只先提交 2 个。"""
    f, bot, client = _fetcher(tmp_path, max_tasks=2,
                              files=[{"name": "季", "size": 0, "is_dir": True}])
    client.items[f"{_MON}/季"] = [
        {"name": f"E{i:02d}.mkv", "size": 10, "is_dir": False} for i in (1, 2, 3)]
    asyncio.run(f.scan_now())
    assert len(client.moves) == 2                                   # 占满并发位即停
    assert len(bot.store.list_fetch("moving")) == 2
    client.undone[0].update({"state": 2})                           # 放掉一个
    client.done.append(client.undone.pop(0))
    asyncio.run(f._poll_tasks())
    asyncio.run(f._scan_dirs())
    assert len(client.moves) == 3                                   # 第三个补上


def test_empty_source_folder_is_pruned(tmp_path):
    """目录文件都搬走后空源目录被清理;还有内容则不动。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "季", "size": 0, "is_dir": True}])
    client.items[f"{_MON}/季"] = [{"name": "E01.mkv", "size": 10, "is_dir": False}]

    async def move(src_dir, dst_dir, names):
        client.moves.append((src_dir, dst_dir, list(names)))
        client.items[src_dir] = [i for i in client.items[src_dir] if i["name"] not in set(names)]
        return [{"id": "T1", "name": "move", "state": 1, "progress": 0}]

    client.move = move
    asyncio.run(f.scan_now())
    assert "季" not in {i["name"] for i in client.items[_MON]}       # 空壳已清

    # 目录内还有非视频文件 → 不删
    client.items[f"{_MON}/留着"] = [
        {"name": "readme.txt", "size": 1, "is_dir": False},
        {"name": "video.mkv", "size": 10, "is_dir": False}]
    client.items[_MON].append({"name": "留着", "size": 0, "is_dir": True})
    client.items[f"{_MON}/留着/video.mkv"] = []
    (bot.cfg.openlist_dir / "留着").mkdir(parents=True, exist_ok=True)
    (bot.cfg.openlist_dir / "留着" / "video.mkv").write_bytes(b"x")   # 视频已本地存在 → 跳过
    asyncio.run(f.scan_now())
    assert "留着" in {i["name"] for i in client.items[_MON]}


# ── 补删重试 + 源目录清理(2026-09-12) ────────────────────────
def test_should_fetch_skips_cleanup_status(tmp_path):
    """`cleanup` 状态必须与 moving/done 一起跳过。

    回归锁:只跳 moving/done 时,处于 cleanup 的源文件(**仍在监控目录里**)会被再搬一次
    —— 本地文件这时已被处理段移走,`_local_path` 兜底救不了 → 重复下载 + 重复处理 + 115 重复入库。
    """
    f, bot, _ = _fetcher(tmp_path)
    src = f"{_MON}/a.mkv"
    bot.store.save_fetch(src, 1000, status="cleanup", attempts=1, error="remove 500")
    assert f._should_fetch(src, 1000, "a.mkv") is False


def test_verify_source_gone_failure_marks_cleanup_without_notify(tmp_path):
    """补删第一次失败:记成 cleanup 待后续重试,**不当场打扰人**。"""
    f, bot, client = _fetcher(tmp_path)
    src = f"{_MON}/S01/a.mkv"
    client.items[f"{_MON}/S01"] = [{"name": "a.mkv", "size": 1000, "is_dir": False}]

    async def boom(dir_path, names):
        raise OpenListError("/api/fs/remove 返回 code=500:inner error")

    client.remove = boom
    asyncio.run(f._verify_source_gone({"src_path": src, "src_size": 1000, "attempts": 0}))

    saved = bot.store.get_fetch(src)
    assert saved["status"] == "cleanup" and saved["attempts"] == 1
    assert bot.notified == []


def test_cleanup_retry_succeeds_and_prunes_dir(tmp_path):
    """重试成功 → 状态回到 done,并顺手把"没视频没字幕"的源目录清掉。"""
    f, bot, client = _fetcher(tmp_path, files=[{"name": "S01", "size": 0, "is_dir": True}])
    src = f"{_MON}/S01/a.mkv"
    client.items[f"{_MON}/S01"] = [
        {"name": "a.mkv", "size": 1000, "is_dir": False},     # 残留的源文件
        {"name": "a.nfo", "size": 10, "is_dir": False},       # 杂物(不算媒体)
    ]
    bot.store.save_fetch(src, 1000, status="cleanup", attempts=1, error="500")

    asyncio.run(f._retry_cleanup({"done": 0, "failed": 0}))

    assert bot.store.get_fetch(src)["status"] == "done"
    assert client.items[_MON] == []        # S01 整个被清掉(只剩 .nfo,无视频/字幕)
    assert bot.notified == []              # 自己清干净了,不用惊动人


def test_cleanup_gives_up_after_max_attempts_then_notifies(tmp_path):
    """重试到头仍失败:放弃(置 done)+ 通知人工一次,不再空转。"""
    from app.fetcher import CLEANUP_MAX_ATTEMPTS

    f, bot, client = _fetcher(tmp_path)
    src = f"{_MON}/S01/a.mkv"

    async def boom(dir_path, names):
        raise OpenListError("code=500:inner error")

    client.remove = boom
    bot.store.save_fetch(src, 1000, status="cleanup",
                         attempts=CLEANUP_MAX_ATTEMPTS - 1, error="500")

    asyncio.run(f._retry_cleanup({"done": 0, "failed": 0}))

    assert bot.store.get_fetch(src)["status"] == "done"      # 收尾,不再挂着
    assert len(bot.notified) == 1 and "删不掉" in bot.notified[0]


def test_prune_keeps_dirs_with_video_or_subtitle(tmp_path):
    """判据:**子树里还有视频或字幕就保留**;只剩 .nfo/.jpg 的删掉(自底向上级联)。"""
    f, bot, client = _fetcher(tmp_path, files=[
        {"name": "有视频", "size": 0, "is_dir": True},
        {"name": "有字幕", "size": 0, "is_dir": True},
        {"name": "只有图", "size": 0, "is_dir": True},
        {"name": "父", "size": 0, "is_dir": True},
    ])
    client.items[f"{_MON}/有视频"] = [{"name": "a.mkv", "size": 1, "is_dir": False}]
    client.items[f"{_MON}/有字幕"] = [{"name": "b.srt", "size": 1, "is_dir": False}]
    client.items[f"{_MON}/只有图"] = [{"name": "c.jpg", "size": 1, "is_dir": False}]
    client.items[f"{_MON}/父/子"] = [{"name": "d.nfo", "size": 1, "is_dir": False}]
    client.items[f"{_MON}/父"] = [{"name": "子", "size": 0, "is_dir": True}]

    for folder in ("有视频", "有字幕", "只有图", "父"):
        asyncio.run(f._prune_source_dir(_MON, folder))

    names = {i["name"] for i in client.items[_MON]}
    assert "有视频" in names        # 有视频 → 留
    assert "有字幕" in names        # 有字幕 → 留
    assert "只有图" not in names    # 只剩图 → 删
    assert "父" not in names        # 子目录被删后父也空了 → 级联删掉


def test_prune_never_touches_paths_outside_monitor(tmp_path):
    """不在监控目录里的路径:一律不动(绝不越界删别处的目录)。"""
    f, bot, client = _fetcher(tmp_path)
    other = "/别的目录"
    client.items[other] = [{"name": "x", "size": 0, "is_dir": True}]
    client.items[f"{other}/x"] = [{"name": "a.nfo", "size": 1, "is_dir": False}]

    asyncio.run(f._prune_for_file(f"{other}/x/a.mkv"))

    assert client.items[other]                 # 监控目录之外原样保留
    assert f"{other}/x" in client.items
