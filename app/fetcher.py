"""获取段:监控 openlist 目录 → 把新资源 **移动** 到本地落地点(media/openlist)。

链路(用户 2026-09-11 定的"自动流",不做手动投喂):
  ① 轮询配置的 openlist 监控目录(`POST /api/fs/list`)
  ② 发现新条目 → `POST /api/fs/move` 移到 `openlist_dest_path`(实测:跨存储=异步流式任务,
     并把源**移走**——这是"移动"而非复制的语义,用户明确要求)
  ②b **目录逐文件提交(用户 2026-09-11 定)**:实测把目录整体交给 openlist 时,它内部会按
     子目录/文件**扇出多个并发任务**(线程数 `move_task_threads_num`,默认 5)——我们数 1 个、
     它跑 5 路,"并发上限"名存实亡。故改为 **Stow 自己递归展开目录、逐文件提交**:
     每个文件占一个并发位,并发粒度完全由我们控制(落地仍保持相对子目录结构);
     搬空后的源目录会清理掉(仅在确认为空时删)
  ②c **线程数同步**:启动时把 openlist 的 `move_task_threads_num` 设为 `openlist_max_tasks`,
     与我们的闸门口径一致(会一并影响 openlist 里的手动操作,用户已同意自动同步)
  ③ 并发闸门:同时在途的搬运任务 ≤ `openlist_max_tasks`(默认 2,用户要求);逐文件提交后
     该计数与 openlist 的真实任务数一一对应
  ④ 完成判定:**任务出现在 `/api/task/move/done` 且 state==2**(不再自造"文件是否稳定"的启发式)
  ⑤ 完成 → 文件已在 `media/openlist`(=/项目测试 挂载),交给处理段(下一阶段接)
     失败 → 记 error + 私聊通知 + 退避重试(源文件未被移走,可重发)

去重与重启安全:`fetch_state` 表按源路径记录(task_id/status/attempts),重启后继续跟踪。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

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
        self._progress: dict[str, float] = {}   # 文件名 → 百分比(最近一次任务查询)
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()  # 结算串行(扫描与轮询两条路都会结算)

    # ── 生命周期 ────────────────────────────────────────────
    def progress_snapshot(self) -> dict[str, float]:
        """在途搬运的进度快照(name → 百分比),供总览页展示(不触网,读缓存)。"""
        return dict(self._progress)

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

    async def sync_openlist_threads(self) -> None:
        """把 openlist 的任务线程数同步成我们的并发上限(否则它会自己扇出多路并发)。"""
        if self.client is None:
            return
        want = str(max(1, int(self.bot.cfg.openlist_max_tasks)))
        try:
            cur = await self.client.settings()
        except OpenListError as exc:
            logger.warning("读 openlist 设置失败,跳过线程数同步:%s", exc)
            return
        for key in ("move_task_threads_num",):
            old = cur.get(key, "")
            if not old or old == want:
                continue
            try:
                await self.client.save_setting(key, want)
                logger.info("获取段:openlist %s 已由 %s 同步为 %s(与并发上限一致)", key, old, want)
            except OpenListError as exc:
                logger.warning("同步 openlist %s 失败:%s", key, exc)

    async def run_loop(self) -> None:
        interval = max(60, int(self.bot.cfg.fetch_interval_minutes) * 60)
        await self.sync_openlist_threads()
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
                    # 腾出并发位就立刻补提交,别等下一轮扫描(实测轮间隔会让吞吐白等 5 分钟)
                    if self._active_count() < max(1, int(self.bot.cfg.openlist_max_tasks)):
                        await self._scan_dirs()
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
        submitted, skipped = await self._scan_dirs()
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

    async def _scan_dirs(self) -> tuple[int, int]:
        """扫全部监控目录;返回 (提交数, 跳过数)。"""
        submitted = skipped = 0
        for path in self.bot.cfg.openlist_monitor_list():
            try:
                st = await self._scan_dir(path)
            except OpenListError as exc:
                logger.warning("获取段扫描目录失败(%s):%s", path, exc)
                continue
            submitted += st["submitted"]
            skipped += st["skipped"]
        return submitted, skipped

    async def _scan_dir(self, path: str) -> dict:
        """扫一个监控目录:文件逐个提交;**目录递归展开成逐文件**(并发粒度=文件)。"""
        stat = {"submitted": 0, "skipped": 0}
        if self.client is None:
            return stat
        for it in await self.client.list_dir(path):
            name = it.get("name")
            if not name:
                continue
            if it.get("is_dir"):
                sub = await self._scan_subtree(path, name)
                stat["submitted"] += sub["submitted"]
                stat["skipped"] += sub["skipped"]
                await self._prune_source_dir(path, name)
                continue
            ok = await self._submit_one_file(path, name, int(it.get("size") or 0),
                                            dest_dir=self.bot.cfg.openlist_dest_path)
            stat["submitted" if ok else "skipped"] += 1
            if ok and self._at_limit():
                break
        return stat

    def _at_limit(self) -> bool:
        limit = max(1, int(self.bot.cfg.openlist_max_tasks))
        return self._active_count() >= limit

    async def _submit_one_file(self, src_dir: str, name: str, size: int, *, dest_dir: str) -> bool:
        """提交单个文件的移动(保持相对子目录);已处理/达并发上限则跳过。"""
        if self.client is None:
            return False
        src = f"{src_dir.rstrip('/')}/{name}"
        if not self._should_fetch(src, size, name):
            return False
        if self._at_limit():
            logger.info("获取段:在途任务已达上限 %d,本轮不再提交", self.bot.cfg.openlist_max_tasks)
            return False
        try:
            tasks = await self.client.move(src_dir, dest_dir, [name])
        except OpenListError as exc:
            logger.warning("获取段提交移动失败(%s):%s", src, exc)
            return False
        task_id = str((tasks[0].get("id") if tasks else "") or "")
        self._save(src, size=size, task_id=task_id, status="moving", error="")
        logger.info("获取段已提交移动:%s → %s(任务 %s)", src, dest_dir, task_id or "立即完成")
        return True

    async def _scan_subtree(self, base: str, folder: str) -> dict:
        """递归展开目录:每个文件单独提交(相对子目录结构原样保留)。"""
        stat = {"submitted": 0, "skipped": 0}
        if self.client is None:
            return stat
        dest_root = self.bot.cfg.openlist_dest_path
        stack = [(f"{base.rstrip('/')}/{folder}", f"{dest_root.rstrip('/')}/{folder}")]
        while stack:
            cur_src, cur_dest = stack.pop(0)
            for it in await self.client.list_dir(cur_src):
                name = it.get("name")
                if not name:
                    continue
                if it.get("is_dir"):
                    stack.append((f"{cur_src}/{name}", f"{cur_dest}/{name}"))
                    continue
                ok = await self._submit_one_file(cur_src, name, int(it.get("size") or 0),
                                                 dest_dir=cur_dest)
                stat["submitted" if ok else "skipped"] += 1
                if ok and self._at_limit():
                    logger.info("获取段:在途任务已达上限 %d,该目录剩余文件下轮继续",
                                self.bot.cfg.openlist_max_tasks)
                    return stat
        return stat

    async def _prune_source_dir(self, base: str, folder: str) -> None:
        """源目录搬空后删掉空壳(自底向上;仅在**确认为空**时删,留了文件就不动)。"""
        if self.client is None:
            return
        root = f"{base.rstrip('/')}/{folder}"
        dirs: list[str] = []
        stack = [root]
        while stack:
            cur = stack.pop()
            dirs.append(cur)
            try:
                for it in await self.client.list_dir(cur):
                    if it.get("is_dir"):
                        stack.append(f"{cur}/{it['name']}")
            except OpenListError as exc:
                logger.debug("列源子目录失败(%s):%s", cur, exc)
        for d in sorted(set(dirs), key=len, reverse=True):
            parent, _, name = d.rpartition("/")
            if not name:
                continue
            try:
                if await self.client.list_dir(d):
                    continue                      # 仍有内容(如非视频文件):不动
                await self.client.remove(parent or "/", [name])
                logger.info("获取段:源目录已搬空并清理:%s", d)
            except OpenListError as exc:
                logger.debug("清理空源目录失败(%s):%s", d, exc)

    def _local_path(self, src_path: str) -> Path:
        """源路径 → 本地落地点路径(**保持相对子目录结构**)。

        逐文件提交时落地结构是 `<落地点>/<目录>/<文件>`,不能只用文件名去找
        ——否则目录内文件会被误判成"本地不存在"而反复搬(实测踩到)。
        """
        base = Path(self.bot.cfg.openlist_dir)
        for mon in self.bot.cfg.openlist_monitor_list():
            m = mon.rstrip("/")
            if src_path.startswith(m + "/"):
                return base / src_path[len(m) + 1:]
        return base / src_path.rsplit("/", 1)[-1]

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
        # 本地已存在 → 说明已经搬进来了(人工搬过、或重启前搬完),直接记账完成。
        # 目录不做大小比对:openlist 报目录 size=0,而本地目录 st_size 随文件系统变
        # (drvfs 512 / ext4 4096),比了必然失配 → 反复提交
        local = self._local_path(src)
        try:
            if local.exists() and (local.is_dir() or local.stat().st_size == size):
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

        # 刷新进度快照(总览页展示用):move 任务没有源路径字段,用文件名包含匹配
        snap: dict[str, float] = {}
        for row in moving:
            base = row["src_path"].rsplit("/", 1)[-1]
            t = next((x for x in undone if base in str(x.get("name") or "")), None)
            if t is not None:
                snap[base] = round(float(t.get("progress") or 0), 1)
        self._progress = snap

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
        """目标是否已在落地点(完成判定的兜底)。

        目录只判存在:openlist 报目录 size=0,本地目录 st_size 因文件系统而异
        (drvfs 512 / ext4 4096),按 size 比会对不上。
        """
        local = self._local_path(row["src_path"])
        try:
            if not local.exists():
                return False
            return local.is_dir() or local.stat().st_size == int(row["src_size"] or 0)
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
            logger.info("获取段完成:%s → 本地落地点(已交处理段)",
                        row["src_path"])
            await self._verify_source_gone(row)
            return
        logger.warning("获取段失败(%s,第 %d 次):%s", row["src_path"], attempts, error)
        text = (f"⚠️ 获取失败:{row['src_path']}\n原因:{error or '未知'}\n"
                f"已尝试 {attempts} 次(上限 {MAX_ATTEMPTS});源文件未被移走,可重试。")
        asyncio.create_task(self._notify(text))

    async def _verify_source_gone(self, row: dict) -> None:
        """校验"移动"是否真的删掉了源。

        实测(2026-09-11):openlist 偶发**任务报成功但源文件仍在**(夸克侧删源那步没做成),
        留个残影在源目录里。这里补删一次并告警;补删也失败则通知人工(不会反复重试)。
        """
        if self.client is None:
            return
        parent, _, name = row["src_path"].rpartition("/")
        try:
            items = await self.client.list_dir(parent or "/")
        except OpenListError as exc:
            logger.warning("获取段:校验源是否删除失败(%s):%s", row["src_path"], exc)
            return
        if name not in {it.get("name") for it in items}:
            return
        logger.warning("获取段:openlist 报移动成功但源仍在,补删一次:%s", row["src_path"])
        try:
            await self.client.remove(parent or "/", [name])
            logger.info("获取段已补删源:%s", row["src_path"])
        except OpenListError as exc:
            logger.error("获取段补删源失败(%s):%s", row["src_path"], exc)
            await self._notify(
                f"⚠️ 源文件删不掉(openlist 报移动成功但源仍在):{row['src_path']}\n"
                f"原因:{str(exc)[:120]}\n可到 openlist 里手动删除。")

    async def _notify(self, text: str) -> None:
        if self.bot.cfg.tg_admin_ids:
            await self.bot._notify_uid(self.bot.cfg.tg_admin_ids[0], text)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
