"""获取段:监控 openlist 目录 → 把新资源 **移动** 到本地落地点(media/openlist)。

链路(用户 2026-09-11 定的"自动流",不做手动投喂):
  ① 轮询配置的 openlist 监控目录(`POST /api/fs/list`)
  ② 发现新条目 → `POST /api/fs/move` 移到 `openlist_dest_path`(实测:跨存储=异步流式任务,
     并把源**移走**——这是"移动"而非复制的语义,用户明确要求)
  ③ 并发闸门:同时进行中的搬运任务 ≤ `openlist_max_tasks`(默认 2,用户要求)
  ④ 完成判定:**任务出现在 `/api/task/move/done` 且 state==2**(不再自造"文件是否稳定"的启发式)
  ⑤ 完成 → 文件已在 `media/openlist`(=/项目测试 挂载),交给处理段(下一阶段接)
     失败 → 记 error + 私聊通知 + 退避重试(源文件未被移走,可重发)

去重与重启安全:`fetch_state` 表按源路径记录(task_id/status/attempts),重启后继续跟踪。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.openlist import TASK_SUCCEEDED, OpenListClient, OpenListError

logger = logging.getLogger(__name__)

POLL_EVERY_SECONDS = 20    # 任务状态结算间隔(与大轮扫描解耦:搬运通常一两分钟就完)
MAX_ATTEMPTS = 3           # 单个条目最多重试次数(超过则放弃并提示人工)
RETRY_BACKOFF_SECONDS = 300  # 失败后至少等这么久再重试(避免同一轮/连续轮空转)
TASK_LOST_SECONDS = 1800   # 任务在列表里消失且目标文件也不在:判定任务丢失的时间阈值


class ResourceFetcher:
    """openlist → 本地 的自动搬运;持有 bot 复用其 cfg/store/通知通道。"""

    def __init__(self, bot) -> None:
        self.bot = bot
        cfg = bot.cfg
        self.client: OpenListClient | None = (
            OpenListClient(cfg.openlist_base_url, cfg.openlist_token)
            if cfg.openlist_base_url and cfg.openlist_token else None
        )
        self._loop_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()  # 结算串行(扫描与轮询两条路都会结算)

    # ── 生命周期 ────────────────────────────────────────────
    def enabled(self) -> bool:
        cfg = self.bot.cfg
        return bool(self.client and cfg.openlist_monitor_list())

    def start(self) -> None:
        if not self.enabled():
            logger.info("获取段未启动:未配置 openlist 地址/令牌或监控目录")
            return
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self.run_loop())
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self.poll_loop())

    async def run_loop(self) -> None:
        interval = max(60, int(self.bot.cfg.fetch_interval_minutes) * 60)
        logger.info("获取段已启动:监控 %s,并发上限 %d,每 %d 分钟扫一轮、每 %d 秒结算任务",
                    self.bot.cfg.openlist_monitor_list(), self.bot.cfg.openlist_max_tasks,
                    self.bot.cfg.fetch_interval_minutes, POLL_EVERY_SECONDS)
        while True:
            try:
                await self.scan_now()
            except Exception as exc:  # noqa: BLE001 - 单轮异常不拖垮循环
                logger.error("获取段本轮异常:%s", exc, exc_info=exc)
            await asyncio.sleep(interval)

    async def poll_loop(self) -> None:
        """任务结算循环(独立于大轮扫描):搬运完成即刻落账,尽快腾出并发位。"""
        while True:
            await asyncio.sleep(POLL_EVERY_SECONDS)
            try:
                settled = await self._poll_tasks()
                if settled["done"] or settled["failed"]:
                    logger.info("获取段结算:完成 %d,失败 %d", settled["done"], settled["failed"])
            except Exception as exc:  # noqa: BLE001 - 单轮异常不拖垮循环
                logger.warning("获取段任务结算异常:%s", exc)

    # ── 一轮:先结算在途任务,再发现并提交新资源 ─────────────
    async def scan_now(self) -> str:
        """扫一轮(手动 /fetch 与自动轮共用);返回给用户看的报告摘要。"""
        if not self.enabled():
            return "未启用获取段:请先在全局配置页填 openlist 地址/令牌与监控目录。"
        if self.client is None:
            return "未配置 openlist 客户端。"
        settled = await self._poll_tasks()
        submitted, skipped = 0, 0
        for path in self.bot.cfg.openlist_monitor_list():
            try:
                st = await self._scan_dir(path)
            except OpenListError as exc:
                logger.warning("获取段扫描目录失败(%s):%s", path, exc)
                continue
            submitted += st["submitted"]
            skipped += st["skipped"]
        done, failed = settled["done"], settled["failed"]
        head = f"📥 获取段报告:提交 {submitted} 个"
        if done:
            head += f",完成 {done} 个"
        if failed:
            head += f",失败 {failed} 个"
        if skipped:
            head += f",跳过 {skipped} 个(已处理/在途/超上限)"
        pending = len(self.bot.store.list_fetch("moving"))
        if pending:
            head += f"\n⏳ 在途 {pending} 个(并发上限 {self.bot.cfg.openlist_max_tasks})"
        return head

    async def _scan_dir(self, path: str) -> dict:
        """扫一个监控目录:新条目 → 移动任务(受并发闸门约束)。"""
        stat = {"submitted": 0, "skipped": 0}
        items = await self.client.list_dir(path) if self.client else []
        for it in items:
            name = it.get("name")
            if not name:
                continue
            src = f"{path.rstrip('/')}/{name}"
            if not self._should_fetch(src, int(it.get("size") or 0), name):
                stat["skipped"] += 1
                continue
            if self._active_count() >= max(1, int(self.bot.cfg.openlist_max_tasks)):
                stat["skipped"] += 1
                logger.info("获取段:在途任务已达上限 %d,本轮不再提交", self.bot.cfg.openlist_max_tasks)
                break
            try:
                tasks = await self.client.move(path, self.bot.cfg.openlist_dest_path, [name])
            except OpenListError as exc:
                logger.warning("获取段提交移动失败(%s):%s", src, exc)
                continue
            task_id = str((tasks[0].get("id") if tasks else "") or "")
            local_size = int(it.get("size") or 0)
            self._save(src, size=local_size, task_id=task_id, status="moving", error="")
            stat["submitted"] += 1
            logger.info("获取段已提交移动:%s → %s(任务 %s)",
                        src, self.bot.cfg.openlist_dest_path, task_id or "立即完成")
        return stat

    def _should_fetch(self, src: str, size: int, name: str) -> bool:
        """是否该为这个源条目提交搬运。"""
        row = self.bot.store.get_fetch(src)
        if row:
            if row["status"] in ("moving", "done"):
                return False                       # 在途 / 已完成:跳过
            if row["status"] == "failed":
                if row["attempts"] >= MAX_ATTEMPTS:
                    return False                   # 重试超限:交人工
                if time.time() - float(row["updated_at"] or 0) < RETRY_BACKOFF_SECONDS:
                    return False                   # 退避中:本轮不重试
        # 本地已存在同名同大小 → 说明已经搬进来了(人工搬过、或重启前搬完),直接记账完成
        local = self.bot.cfg.openlist_dir / name
        try:
            if local.exists() and local.stat().st_size == size:
                self._save(src, size=size, status="done", error="", task_id="")
                logger.info("获取段跳过(本地已存在同大小):%s", src)
                return False
        except OSError as exc:  # noqa: BLE001
            logger.debug("查本地文件失败(%s):%s", local, exc)
        return True

    def _active_count(self) -> int:
        """在途搬运任务数(以库为准:重启后接着算)。"""
        return len(self.bot.store.list_fetch("moving"))

    def _save(self, src: str, *, size: int, status: str, task_id: str = "",
              error: str = "", attempts: int | None = None) -> None:
        row = self.bot.store.get_fetch(src) or {}
        self.bot.store.save_fetch(
            src, size,
            dest_path=self.bot.cfg.openlist_dest_path,
            task_id=task_id or str(row.get("task_id") or ""),
            status=status,
            attempts=int(row.get("attempts") or 0) if attempts is None else attempts,
            error=error,
        )

    # ── 结算在途任务(完成判定看任务状态) ────────────────────
    async def _poll_tasks(self) -> dict:
        """结算在途任务;两条循环(扫描/轮询)都会调用,加锁串行。"""
        async with self._poll_lock:
            return await self._poll_once()

    async def _poll_once(self) -> dict:
        settled = {"done": 0, "failed": 0}
        moving = self.bot.store.list_fetch("moving")
        if not moving or self.client is None:
            return settled
        try:
            undone = await self.client.move_tasks(undone=True)
            done = await self.client.move_tasks(undone=False)
        except OpenListError as exc:
            logger.warning("获取段查询任务状态失败:%s", exc)
            return settled

        undone_ids = {str(t.get("id")) for t in undone}
        done_by_id = {str(t.get("id")): t for t in done}
        for row in moving:
            tid = row["task_id"]
            if tid and tid in undone_ids:
                continue                                   # 仍在跑
            task = done_by_id.get(tid) if tid else None
            if task is not None:
                state = int(task.get("state") or 0)
                if state == TASK_SUCCEEDED:
                    await self._finish(row, "done", settled)
                else:
                    await self._finish(row, "failed", settled,
                                       error=str(task.get("error") or task.get("status")
                                                 or f"state={state}"))
                continue
            # 两个列表都查不到:核验目标是否已落地(小文件任务可能秒完成且不留痕)
            if self._landed(row):
                await self._finish(row, "done", settled)
            elif time.time() - float(row["updated_at"] or 0) > TASK_LOST_SECONDS:
                await self._finish(row, "failed", settled,
                                   error="任务在 openlist 列表中丢失")
        return settled

    def _landed(self, row: dict) -> bool:
        """目标文件是否已在落地点且大小相符(完成判定的兜底校验)。"""
        name = row["src_path"].rsplit("/", 1)[-1]
        local = self.bot.cfg.openlist_dir / name
        try:
            return local.exists() and local.stat().st_size == int(row["src_size"] or 0)
        except OSError:
            return False

    async def _finish(self, row: dict, status: str, settled: dict,
                     error: str = "") -> None:
        """结算一条搬运:成功记 done;失败计数 + 通知(源文件还在,可重试)。"""
        attempts = int(row["attempts"] or 0) + (1 if status == "failed" else 0)
        self._save(row["src_path"], size=int(row["src_size"] or 0), status=status,
                   error=error, attempts=attempts)
        settled["done" if status == "done" else "failed"] += 1
        if status == "done":
            logger.info("获取段完成:%s → %s(等待处理段接入)",
                        row["src_path"], self.bot.cfg.openlist_dir / row["src_path"].rsplit("/", 1)[-1])
            return
        logger.warning("获取段失败(%s,第 %d 次):%s", row["src_path"], attempts, error)
        text = (f"⚠️ 获取失败:{row['src_path']}\n原因:{error or '未知'}\n"
                f"已尝试 {attempts} 次(上限 {MAX_ATTEMPTS});源文件未被移走,可重试。")
        asyncio.create_task(self._notify(text))

    async def _notify(self, text: str) -> None:
        if self.bot.cfg.tg_admin_ids:
            await self.bot._notify_uid(self.bot.cfg.tg_admin_ids[0], text)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
