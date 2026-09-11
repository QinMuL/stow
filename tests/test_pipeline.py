"""流水线任务持久化测试(v0.2 P0):重启丢任务 → 目录重扫重复建分享 的回归锁。

不触网:reader/saver/tmdb/normalizer 全用替身。
"""

from __future__ import annotations

import asyncio
import time

from app.bot import PushResult
from app.config import Config
from app.normalizer import NormalizeResult
from app.pipeline import PipelineTask, SavePipeline
from app.store import Store


# ── 替身 ────────────────────────────────────────────────────
class FakeReader:
    logged_in = True

    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = items or []
        self.created: list[int] = []
        self.moved: list[tuple[int, int]] = []
        self.status = {"violating": False, "expired": False, "auditing": False}

    async def list_dir(self, cid, nf=0):
        return self.items

    async def find_dir(self, path):
        return 100

    async def create_share(self, fid):
        self.created.append(fid)
        return f"code{fid}", f"pwd{fid}"

    async def share_status(self, code, password=None):
        return self.status

    async def read_share(self, link):
        from app.pan115 import ShareDead

        raise ShareDead("已失效")

    async def fs_move(self, fid, dest):
        self.moved.append((fid, dest))


class FakeSaver:
    async def ensure_dir(self, name, parent_cid=None):
        return 999

    async def save_share(self, link, parent_cid=None):
        raise AssertionError("本测试不应真转存")


class FakeNormalizer:
    def __init__(self, fid: int, name: str = "剧名 (2024) {tmdb-1}") -> None:
        self.fid, self.name = fid, name
        self.calls: list[int] = []

    async def normalize(self, fid, name, is_dir, parent_cid):
        self.calls.append(fid)
        return NormalizeResult(fid=self.fid, name=self.name, changed=False, actions=["测试"])


class FakeBot:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.reader = FakeReader()
        self.saver = FakeSaver()
        self.tmdb = None
        self.notified: list[str] = []
        self.delivered: list = []

    async def _notify_uid(self, uid, text):
        self.notified.append(text)

    async def _deliver(self, media, details, link, files, target):
        self.delivered.append((link.dedup_display, target))
        return PushResult(True, "✅ 已推送")


def _bot(tmp_path, *, monitor_dirs="") -> FakeBot:
    cfg = Config(
        data_dir=str(tmp_path), monitor_dirs=monitor_dirs, tg_admin_ids=[5406565010],
        pipeline_root_dir="3515046004502037692",
        channels=[],  # 归属频道:channels_for 返回空表 → 推送中止(本测试只关心状态)
    )
    return FakeBot(cfg, Store(tmp_path / "t.db"))


def _seed_task(store: Store, share_code="swsabc123", fid=777, status="auditing",
               uid=5406565010) -> PipelineTask:
    task = PipelineTask(share_code=share_code, receive_code="abcd", fid=fid,
                        name="剧名 (2024) {tmdb-1}", uid=uid, status=status)
    store.save_pipeline_task(task.to_row())
    return task


# ── 存储层 ──────────────────────────────────────────────────
def test_pipeline_task_roundtrip(tmp_path):
    st = Store(tmp_path / "t.db")
    _seed_task(st)
    rows = st.load_pipeline_tasks(("auditing",))
    assert len(rows) == 1
    t = PipelineTask.from_row(rows[0])
    assert (t.share_code, t.fid, t.receive_code, t.name) == (
        "swsabc123", 777, "abcd", "剧名 (2024) {tmdb-1}")
    assert st.pipeline_task_stats() == {"auditing": 1}
    st.close()


def test_pipeline_task_upsert_is_idempotent(tmp_path):
    st = Store(tmp_path / "t.db")
    _seed_task(st)
    _seed_task(st, status="auditing")          # 同 share_code 再写
    assert len(st.load_pipeline_tasks(("auditing",))) == 1
    st.close()


def test_load_only_auditing(tmp_path):
    """已结束的任务不载入(目录已移走,重扫不会再遇到)。"""
    st = Store(tmp_path / "t.db")
    _seed_task(st, share_code="a", status="auditing")
    _seed_task(st, share_code="b", status="done")
    _seed_task(st, share_code="c", status="violated")
    _seed_task(st, share_code="d", status="timeout")
    assert [r["share_code"] for r in st.load_pipeline_tasks(("auditing",))] == ["a"]
    assert st.pipeline_task_stats() == {"auditing": 1, "done": 1, "violated": 1, "timeout": 1}
    st.close()


# ── 重启恢复 ────────────────────────────────────────────────
def test_restart_resumes_auditing_task(tmp_path):
    """重启后:审核中的任务自动载入继续轮询(修复"重启丢任务")。"""
    bot = _bot(tmp_path)
    _seed_task(bot.store, share_code="swskeep01", fid=555)
    pipeline = SavePipeline(bot)                       # = 重启后的新实例
    assert "swskeep01" in pipeline.tasks
    assert pipeline.tasks["swskeep01"].fid == 555
    assert pipeline.tasks["swskeep01"].created_at <= time.time()


def test_rescan_after_restart_skips_known_fid(tmp_path):
    """核心回归锁:重启后目录监控重扫,已知 fid 不再重复建分享。"""
    bot = _bot(tmp_path, monitor_dirs="100")
    bot.reader.items = [{"fid": 555, "name": "剧名 (2024)", "is_dir": True}]
    _seed_task(bot.store, share_code="swskeep01", fid=555)
    pipeline = SavePipeline(bot)

    stat = asyncio.run(pipeline._scan_monitor_dir("100"))
    assert stat["new"] == 0            # 已登记任务的 fid 直接跳过
    assert bot.reader.created == []    # 没有重复建分享
    assert stat["shared"] == 0


def test_rescan_creates_share_for_new_fid_and_persists(tmp_path):
    """新 fid 照常建分享,并立刻落库(下次重启才不会重复建)。"""
    bot = _bot(tmp_path, monitor_dirs="100")
    bot.reader.items = [{"fid": 888, "name": "新剧 (2024)", "is_dir": True}]
    pipeline = SavePipeline(bot)
    pipeline.normalizer = FakeNormalizer(fid=888)

    stat = asyncio.run(pipeline._scan_monitor_dir("100"))
    assert stat["shared"] == 1 and bot.reader.created == [888]
    rows = bot.store.load_pipeline_tasks(("auditing",))
    assert [r["fid"] for r in rows] == [888]           # 落库了
    assert rows[0]["receive_code"] == "pwd888"


# ── 状态变化落库 ────────────────────────────────────────────
def test_violated_status_persisted(tmp_path):
    bot = _bot(tmp_path)
    task = _seed_task(bot.store, share_code="swsbad001", fid=666)
    bot.reader.status = {"violating": True, "expired": False, "auditing": False}
    pipeline = SavePipeline(bot)
    pipeline.tasks["swsbad001"] = task

    asyncio.run(pipeline._check_task(task))
    assert pipeline.tasks["swsbad001"].status == "violated"
    assert bot.store.pipeline_task_stats() == {"violated": 1}   # 状态已落库
    assert bot.reader.moved and bot.reader.moved[0][0] == 666   # 移入违规目录
    assert any("审核未通过" in t for t in bot.notified)


def test_timeout_status_persisted(tmp_path):
    """超 24h 放弃:状态落库,重启后不会又被载入继续等。"""
    bot = _bot(tmp_path)
    task = _seed_task(bot.store, share_code="swsold001", fid=444)
    task.created_at = time.time() - 25 * 3600
    pipeline = SavePipeline(bot)
    pipeline.tasks["swsold001"] = task

    asyncio.run(pipeline._check_task(task))
    assert task.status == "timeout"
    assert bot.store.pipeline_task_stats() == {"timeout": 1}
    assert any("审核超 24h" in t for t in bot.notified)


def test_auditing_attempts_persisted(tmp_path):
    """审核中:尝试次数递增并落库(便于判断卡了多久)。"""
    bot = _bot(tmp_path)
    task = _seed_task(bot.store, share_code="swswait01", fid=333)
    bot.reader.status = {"violating": False, "expired": False, "auditing": True}
    pipeline = SavePipeline(bot)
    pipeline.tasks["swswait01"] = task

    asyncio.run(pipeline._check_task(task))
    assert task.status == "auditing" and task.attempts == 1
    row = bot.store.load_pipeline_tasks(("auditing",))[0]
    assert row["attempts"] == 1                        # 尝试次数已落库
