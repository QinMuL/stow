"""上传段:把 media/clouddrive 里的成品**移动**到 115(经 CD2 gRPC)。

链路(用户 2026-09-11 定的语义):
  ① 扫 `media/clouddrive` 里的文件
  ② `MoveFile(["/clouddrive/<文件>"], "<cd2_dest_path>")` —— **移动而非复制**:
     跨云(本地 → 115)由 CD2 内部完成下载+上传,**本地源随移动消失**(不必再删源),
     且 115 侧命中秒传时秒级完成
  ③ 串行:同一时刻只跟一个上传任务(旧项目"每轮至多提交一个";避免同时多路占带宽/触发风控)
  ④ 完成判定**只看内容**:任务的 `uploadedBytes == totalBytes` 且 `errors` 为空 = 完成;
     `errors` 非空 = 失败;任务消失且本地源也没了 = 完成(移动语义的天然信号)。
     ⚠️ 不按 TaskStatus 枚举名判定——本机服务器 1.0.13 与官方 proto 1.0.14 的口径不一致
  ⑤ 失败:本地源仍在(移动未成立)→ 退避重试,上限后通知人工

重启安全:`upload_tasks` 表按文件名记状态,重启后继续追踪在途任务。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.cd2.client import Cd2Client, Cd2Error

logger = logging.getLogger(__name__)

POLL_EVERY_SECONDS = 20       # 任务结算间隔
MAX_ATTEMPTS = 3              # 单文件重试上限
RETRY_BACKOFF_SECONDS = 300   # 失败退避(避免连续轮空转)
TASK_LOST_SECONDS = 3600      # 任务消失且源文件仍在:超过此时长判失败


class Uploader:
    """CD2 上传(移动语义);持有 bot 复用其 cfg/store/通知通道。"""

    def __init__(self, bot) -> None:
        self.bot = bot
        cfg = bot.cfg
        self.client: Cd2Client | None = None
        if cfg.cd2_address:
            self.client = Cd2Client(cfg.cd2_address, cfg.cd2_token,
                                    username=cfg.cd2_username, password=cfg.cd2_password)
        self._loop_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()      # 串行闸门 + 结算互斥

    # ── 生命周期 ────────────────────────────────────────────
    def enabled(self) -> bool:
        cfg = self.bot.cfg
        return bool(self.client and cfg.cd2_source_path and cfg.cd2_dest_path)

    def start(self) -> None:
        if not self.enabled():
            missing = []
            if not self.client:
                missing.append("CD2 地址/令牌")
            if not self.bot.cfg.cd2_source_path:
                missing.append("待上传目录(CD2 侧本地视图)")
            if not self.bot.cfg.cd2_dest_path:
                missing.append("上传目标目录")
            logger.info("上传段未启动:未配置 %s", "、".join(missing))
            return
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self.run_loop())
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self.poll_loop())

    async def run_loop(self) -> None:
        interval = max(60, int(self.bot.cfg.upload_interval_minutes) * 60)
        logger.info("上传段已启动:%s → %s,每 %d 分钟一轮、每 %d 秒结算任务",
                    self.bot.cfg.cd2_source_path, self.bot.cfg.cd2_dest_path,
                    self.bot.cfg.upload_interval_minutes, POLL_EVERY_SECONDS)
        while True:
            try:
                await self.scan_now()
            except Exception as exc:  # noqa: BLE001
                logger.error("上传段本轮异常:%s", exc, exc_info=exc)
            await asyncio.sleep(interval)

    async def poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_EVERY_SECONDS)
            try:
                settled = await self._poll_tasks()
                if settled["done"] or settled["failed"]:
                    logger.info("上传段结算:完成 %d,失败 %d", settled["done"], settled["failed"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("上传段结算异常:%s", exc)

    # ── 一轮:结算在途 → 提交下一个(串行) ────────────────────
    async def scan_now(self) -> str:
        if not self.enabled():
            return "未启用上传段:请先在全局配置页填 CD2 待上传目录与上传目标目录。"
        settled = await self._poll_tasks()
        submitted = await self._submit_next()
        head = "📤 上传段"
        if submitted:
            head += f":已提交「{submitted}」"
        elif settled["done"] or settled["failed"]:
            head += f":完成 {settled['done']},失败 {settled['failed']}"
        else:
            head += ":无待上传文件" if not self._pending_names() else ":有任务在途"
        pending = self.bot.store.list_uploads("uploading")
        if pending:
            head += f"\n⏳ 在途 {len(pending)} 个(串行上传)"
        return head

    def _pending_names(self) -> list[str]:
        return [r["name"] for r in self.bot.store.list_uploads("uploading")]

    async def _submit_next(self) -> str:
        """串行:已有在途任务则不提交;否则挑一个未处理的文件提交移动。"""
        async with self._lock:
            if self._pending_names():
                return ""
            src_dir = Path(self.bot.cfg.cd2_source_path)
            local_dir = Path(self.bot.cfg.clouddrive_dir)
            candidates = sorted(
                (p for p in local_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_size,
            )
            for path in candidates:
                row = self.bot.store.get_upload(path.name, path.stat().st_size)
                if self._skip(row):
                    continue
                await self._ensure_dest_dir()
                remote_src = f"{src_dir.as_posix().rstrip('/')}/{path.name}"
                try:
                    res = await asyncio.to_thread(
                        self.client.move_file, [remote_src], self.bot.cfg.cd2_dest_path)
                except Cd2Error as exc:
                    logger.warning("上传提交失败(%s):%s", path.name, exc)
                    continue
                if not res["success"]:
                    logger.warning("上传提交被拒(%s):%s", path.name, res["error"])
                    self._record(path, status="failed", error=res["error"] or "提交被拒")
                    continue
                self._record(path, status="uploading", error="")
                logger.info("上传段已提交移动:%s → %s", remote_src, self.bot.cfg.cd2_dest_path)
                return path.name
            return ""

    def _skip(self, row: dict | None) -> bool:
        if row is None:
            return False
        if row["status"] in ("uploading", "done"):
            return True
        if row["status"] == "failed":
            if row["attempts"] >= MAX_ATTEMPTS:
                return True
            if time.time() - float(row["updated_at"] or 0) < RETRY_BACKOFF_SECONDS:
                return True
        return False

    async def _ensure_dest_dir(self) -> None:
        """目标目录不存在则创建(逐级创建,失败仅告警——MoveFile 会再报错)。"""
        dest = self.bot.cfg.cd2_dest_path.rstrip("/")
        if not dest:
            return
        parent, _, name = dest.rpartition("/")
        if not name:
            return
        parent = parent or "/"
        try:
            existing = {f.name for f in await asyncio.to_thread(self.client.sub_files, parent)}
        except Cd2Error as exc:
            logger.debug("查目标父目录失败(%s):%s", parent, exc)
            return
        if name in existing:
            return
        try:
            await asyncio.to_thread(self.client.create_folder, parent, name)
            logger.info("上传段已创建目标目录:%s", dest)
        except Cd2Error as exc:
            logger.warning("创建目标目录失败(%s):%s", dest, exc)

    # ── 结算:按内容判定(不看枚举名) ─────────────────────────
    async def _poll_tasks(self) -> dict:
        async with self._lock:
            return await self._poll_once()

    async def _poll_once(self) -> dict:
        settled = {"done": 0, "failed": 0}
        rows = self.bot.store.list_uploads("uploading")
        if not rows or self.client is None:
            return settled
        try:
            tasks = await asyncio.to_thread(self.client.copy_tasks)
        except Cd2Error as exc:
            logger.warning("上传段查询任务失败:%s", exc)
            return settled
        src_dir = self.bot.cfg.cd2_source_path.rstrip("/")
        for row in rows:
            remote = f"{src_dir}/{row['name']}"
            task = next((t for t in tasks if t["source"].rstrip("/") == remote), None)
            local = Path(self.bot.cfg.clouddrive_dir) / row["name"]
            if task is not None:
                if task["error"]:
                    await self._finish(row, "failed", settled, error=task["error"])
                elif task["total_bytes"] and task["uploaded_bytes"] >= task["total_bytes"]:
                    await self._finish(row, "done", settled)
                continue
            # 任务已不在列表:移动把源文件带走了 → 视为完成
            if not local.exists():
                await self._finish(row, "done", settled)
            elif time.time() - float(row["updated_at"] or 0) > TASK_LOST_SECONDS:
                await self._finish(row, "failed", settled, error="任务在 CD2 列表中丢失")
        return settled

    async def _finish(self, row: dict, status: str, settled: dict, error: str = "") -> None:
        attempts = int(row["attempts"] or 0) + (1 if status == "failed" else 0)
        self.bot.store.save_upload(row["name"], int(row["size"] or 0), status=status,
                                   dest=self.bot.cfg.cd2_dest_path, error=error,
                                   attempts=attempts)
        settled["done" if status == "done" else "failed"] += 1
        if status == "done":
            logger.info("上传段完成:%s → %s(本地源已随移动消失)", row["name"],
                        self.bot.cfg.cd2_dest_path)
            return
        logger.warning("上传失败(%s,第 %d 次):%s", row["name"], attempts, error)
        if self.bot.cfg.tg_admin_ids:
            await self.bot._notify_uid(
                self.bot.cfg.tg_admin_ids[0],
                f"⚠️ 上传失败:{row['name']}\n原因:{error or '未知'}\n"
                f"已尝试 {attempts} 次(上限 {MAX_ATTEMPTS});本地文件仍在,"
                f"{RETRY_BACKOFF_SECONDS // 60} 分钟后自动重试。")

    def _record(self, path: Path, *, status: str, error: str = "") -> None:
        row = self.bot.store.get_upload(path.name) or {}
        self.bot.store.save_upload(
            path.name, path.stat().st_size if path.exists() else int(row.get("size") or 0),
            status=status, dest=self.bot.cfg.cd2_dest_path, error=error,
            attempts=int(row.get("attempts") or 0))
