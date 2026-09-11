"""/save 转存流水线:转存 → 目录标准化 → 建永久分享 → 审核轮询 → 推送 → 归档。

流程(/save <115链接> 触发,同步段 Bot 回复进度):
  1. 转存分享内容到 暂存目录/任务子目录(Pan115Saver)
  2. 目录结构标准化(ShareNormalizer,Emby 标准结构;失败降级原结构)
  3. 创建永久分享(share_send + duration=-1)
  4. 登记审核轮询任务 —— 115 分享需服务端审核(快慢不定)
审核轮询(audit_loop,2 分钟一轮,上限 24h):
  - 分享可读(审核通过)→ TMDB 匹配 → 推卡到 115 归属频道 → 移入 已发布目录
  - 分享失效/违规 → 不推送 → 移入 违规目录
  - 超时未过 → 放弃并通知,文件留在暂存目录供人工处理

状态在内存:Bot 重启后未完成的流水线任务丢失(文件仍在网盘,可重新 /save)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.links import ParsedLink
from app.media import AggregatedMedia, analyze_share
from app.normalizer import ShareNormalizer
from app.pan115 import ShareDead, ShareError

logger = logging.getLogger(__name__)

_AUDIT_INTERVAL = 120      # 审核轮询间隔(秒)
_MONITOR_INTERVAL = 1800   # 目录监控间隔(秒,30 分钟)
_AUDIT_TIMEOUT = 24 * 3600  # 放弃前最长等待


@dataclass
class PipelineTask:
    """一条 /save 流水线任务(审核轮询阶段的状态)。

    created_at 用**墙钟时间**:时间戳要落库、跨重启比较(_AUDIT_TIMEOUT 按 24h 判)。
    """

    share_code: str
    receive_code: str
    fid: int               # 暂存目录中该资源目录的 CID
    name: str              # 标准化后的资源目录名
    uid: int               # 发起者(TG 通知用)
    status: str = "auditing"  # auditing | done | violated | timeout
    attempts: int = 0
    created_at: float = field(default_factory=time.time)

    def to_row(self) -> dict:
        """落库字段(与 Store.pipeline_tasks 列一致)。"""
        return {
            "share_code": self.share_code, "receive_code": self.receive_code,
            "fid": self.fid, "name": self.name, "uid": self.uid,
            "status": self.status, "attempts": self.attempts,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> PipelineTask:
        return cls(
            share_code=str(row["share_code"]), receive_code=str(row.get("receive_code") or ""),
            fid=int(row.get("fid") or 0), name=str(row.get("name") or ""),
            uid=int(row.get("uid") or 0), status=str(row.get("status") or "auditing"),
            attempts=int(row.get("attempts") or 0),
            created_at=float(row.get("created_at") or time.time()),
        )


class SavePipeline:
    """流水线编排;持有 bot(复用其 reader/tmdb/store/_deliver/回复通道)。"""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.tasks: dict[str, PipelineTask] = {}  # share_code → task
        self.normalizer = ShareNormalizer(bot.reader, bot.tmdb)
        self._loop_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._restore_tasks()

    def _restore_tasks(self) -> None:
        """重启恢复:载入审核中的任务继续轮询(否则目录监控重扫会重复建分享)。

        已结束的任务(done/violated/timeout)不载入——它们的目录已移走,重扫不会再遇到。
        """
        try:
            rows = self.bot.store.load_pipeline_tasks(("auditing",))
        except Exception as exc:  # noqa: BLE001 - 存储异常不阻塞 Bot 启动
            logger.error("载入流水线任务失败:%s", exc, exc_info=exc)
            return
        for row in rows:
            task = PipelineTask.from_row(row)
            self.tasks[task.share_code] = task
        if rows:
            logger.info("载入 %d 个审核中流水线任务,继续轮询(重启不丢)", len(rows))

    def _persist(self, task: PipelineTask) -> None:
        """任务状态落库(失败仅告警:内存态照常跑,不因存储问题中断流水线)。"""
        try:
            self.bot.store.save_pipeline_task(task.to_row())
        except Exception as exc:  # noqa: BLE001
            logger.warning("流水线任务落库失败(%s):%s", task.share_code, exc)

    def start_loop(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._audit_loop())
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self.monitor_loop())

    # ── 同步段:/save 触发 ──────────────────────────────────
    async def handle_save(self, update, link: ParsedLink, prefix: str = "") -> None:
        bot = self.bot
        msg = update.effective_message
        uid = update.effective_user.id
        cfg = bot.cfg

        if link.provider != "115":
            await msg.reply_text(f"{prefix}转存流水线仅支持 115 分享链接(ed2k 无需转存)。")
            return
        if bot.saver is None:
            await msg.reply_text(f"{prefix}⚠️ 未配置 115 Cookie,无法使用转存流水线。")
            return
        # 去重:已推送过 / 已在流水线中(审核/已发布) → 拒绝重复转存
        if bot.store.is_pushed(link.key):
            await msg.reply_text(f"{prefix}🔁 该分享已推送过,无需再次转存。")
            return
        if link.key in bot.pipeline.tasks and bot.pipeline.tasks[link.key].status == "auditing":
            await msg.reply_text(f"{prefix}⏳ 该分享已在流水线中(等待审核),请勿重复提交。")
            return

        label = "115链接推送频道"
        if not cfg.channels_for("115"):
            await msg.reply_text(
                f"{prefix}📭 尚未登记{label},建分享后无处推送。\n"
                "请先登记:发 /bind 后转发频道消息选归属。"
            )
            return

        status = await msg.reply_text(f"{prefix}⏳ [1/3] 正在转存分享内容…")
        try:
            # 流水线根:纯数字=网盘 CID(选择器回填);否则当路径自动创建
            root = cfg.pipeline_root_dir
            root_cid = int(root) if root.strip().isdigit() else await bot.saver.ensure_dir(root)
            staging_cid = await bot.saver.ensure_dir("待整理", parent_cid=root_cid)
            tr = await bot.saver.save_share(link, parent_cid=staging_cid)
        except Exception as exc:  # noqa: BLE001
            logger.error("转存失败 %s:%s", link.code, exc, exc_info=exc)
            await status.edit_text(f"{prefix}❌ 转存失败:{str(exc)[:120]}")
            return
        if not tr.ok:
            await status.edit_text(f"{prefix}❌ 转存失败:{tr.message}")
            return

        await status.edit_text(
            f"{prefix}✅ [2/3] 转存完成({tr.message});正在整理目录结构…"
        )
        normalizer = ShareNormalizer(bot.reader, bot.tmdb)
        nr = await normalizer.normalize(tr.task_cid, tr.task_name, tr.is_dir, staging_cid)
        for a in nr.actions:
            logger.info("流水线标准化:%s", a)
        if not nr.recognized:
            # 无法识别(无 TMDB 命中):不建分享不推送,资源留暂存待人工处理
            logger.warning("流水线中止:资源未识别(%s),不建分享", nr.name)
            await status.edit_text(
                f"{prefix}❓ 无法自动识别该资源(目录名与文件名均无可识别标题,"
                "TMDB 未命中),已保留在暂存目录。请人工重命名后重新 /save,或手动处理。"
            )
            return

        await status.edit_text(f"{prefix}⏳ [3/3] 整理完成;正在创建永久分享…")
        try:
            share_code, receive_code = await bot.reader.create_share(nr.fid)
        except Exception as exc:  # noqa: BLE001
            logger.error("建分享失败 %s:%s", nr.name, exc, exc_info=exc)
            await status.edit_text(f"{prefix}❌ 创建分享失败:{str(exc)[:120]}")
            return

        task = PipelineTask(
            share_code=share_code, receive_code=receive_code,
            fid=nr.fid, name=nr.name, uid=uid,
        )
        self.tasks[share_code] = task
        self._persist(task)  # 落库:重启后继续轮询,不重复建分享
        self.start_loop()
        if nr.actions:
            detail = ";".join(nr.actions[:3]) + ("…" if len(nr.actions) > 3 else "")
        else:
            detail = "结构已标准"
        await status.edit_text(
            f"{prefix}✅ 流水线就绪 · {nr.name}\n"
            f"🔗 分享码 {share_code}(已设永久)\n"
            f"🗂️ 标准化:{detail}\n"
            f"⏳ 115 审核中(通常几分钟~数小时,每 2 分钟自动检查;通过后自动推送到{label})"
        )

    # ── 审核轮询段 ──────────────────────────────────────────
    async def _audit_loop(self) -> None:
        while True:
            await asyncio.sleep(_AUDIT_INTERVAL)
            pending = [t for t in self.tasks.values() if t.status == "auditing"]
            for task in pending:
                try:
                    await self._check_task(task)
                except Exception as exc:  # noqa: BLE001 - 单任务异常不拖垮轮询
                    logger.error("审核轮询任务异常(%s):%s", task.share_code, exc, exc_info=exc)

    # ── 目录监控:监控目录出现新资源 → 标准化 → 建分享 → 登记审核 ──
    async def monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(_MONITOR_INTERVAL)
            dirs = self.bot.cfg.monitor_dir_list()
            if not dirs or self.bot.saver is None:
                continue
            for path in dirs:
                try:
                    await self._scan_monitor_dir(path)
                except Exception as exc:  # noqa: BLE001 - 单目录异常不拖垮轮询
                    logger.error("监控目录扫描异常(%s):%s", path, exc, exc_info=exc)

    async def _scan_monitor_dir(self, path: str) -> dict:
        """扫描监控目录:未标准化处理过的新子项 → 标准化 → 建分享 → 登记审核。

        已处理项会被移入已发布/违规目录,留在监控目录的即为新资源(移动即标记,
        无需额外去重存储);审核中任务记录在内存(tasks),重启后重扫同项会复用
        ——重复建分享的窗口仅在重启后的一个审核周期内,可接受。
        返回统计 {new, shared, skipped, unrecognized}。
        """
        bot = self.bot
        stat = {"new": 0, "shared": 0, "skipped": 0, "unrecognized": 0}
        if bot.reader.logged_in is False:
            return stat
        # 目录值兼容两种形态:纯数字=网盘 CID(选择器回填);路径=只查不建
        # (监控的语义是"盯住已有目录",自动创建空目录没有意义)
        if path.strip().isdigit():
            root_cid = int(path)
        else:
            root_cid = await bot.reader.find_dir(path)
            if root_cid is None:
                logger.warning("监控目录不存在(%s),跳过本轮;请检查路径或重新选择", path)
                stat["skipped"] += 1
                return stat
        items = await bot.reader.list_dir(root_cid, nf=0)
        for it in items:
            fid, name, is_dir = it["fid"], it["name"], it["is_dir"]
            if any(t.fid == fid for t in self.tasks.values()):
                continue  # 审核中,跳过
            stat["new"] += 1
            nr = await self.normalizer.normalize(fid, name, is_dir, root_cid)
            if not nr.recognized:
                stat["unrecognized"] += 1
                logger.warning("监控目录:资源未识别(%s),跳过建分享", name)
                continue
            if nr.actions:
                joined = ";".join(nr.actions[:3])
                suffix = "…" if len(nr.actions) > 3 else ""
                logger.info("监控目录标准化:%s → %s(%s%s)", name, nr.name, joined, suffix)
            share_code, receive_code = await bot.reader.create_share(nr.fid)
            task = PipelineTask(
                share_code=share_code, receive_code=receive_code,
                fid=nr.fid, name=nr.name, uid=bot.cfg.tg_admin_ids[0],
            )
            self.tasks[share_code] = task
            self._persist(task)  # 落库:重启后跳过该 fid,不再重复建分享
            stat["shared"] += 1
            logger.info("监控目录新资源已建分享:%s(%s),进入审核轮询", nr.name, share_code)
            await asyncio.sleep(3)
        return stat

    async def scan_now(self) -> str:
        """手动触发一轮监控扫描(/scan),返回报告文本。"""
        bot = self.bot
        if bot.saver is None or not bot.reader.logged_in:
            return "⚠️ 未配置 115 Cookie,无法扫描(建分享需登录态)。"
        dirs = bot.cfg.monitor_dir_list()
        if not dirs:
            return "未配置监控目录 —— 全局配置 → 目录监控,添加后重试。"
        lines = []
        total_shared = total_unrecognized = 0
        for path in dirs:
            try:
                st = await self._scan_monitor_dir(path)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"• {path}:扫描异常 {str(exc)[:80]}")
                continue
            if st["skipped"]:
                lines.append(f"• {path}:目录不存在,跳过(请检查路径)")
                continue
            line = f"• {path}:新资源 {st['new']} → 建分享 {st['shared']}"
            if st["unrecognized"]:
                line += f",未识别跳过 {st['unrecognized']}"
            lines.append(line)
            total_shared += st["shared"]
            total_unrecognized += st["unrecognized"]
        head = "📂 目录监控报告\n" + "\n".join(lines)
        tail = ""
        if total_shared:
            tail += f"\n⏳ {total_shared} 个新分享进入审核轮询(每 2 分钟检查,通过后自动推送)"
        if total_unrecognized:
            tail += f"\n❓ {total_unrecognized} 个资源未识别,保留在原目录待人工处理"
        return head + tail

    async def _check_task(self, task: PipelineTask) -> None:
        """一轮检查;无论结果如何都把状态/尝试次数落库(重启后接着跑)。"""
        try:
            await self._check_once(task)
        finally:
            self._persist(task)

    async def _check_once(self, task: PipelineTask) -> None:
        bot = self.bot
        cfg = bot.cfg
        if time.time() - task.created_at > _AUDIT_TIMEOUT:
            task.status = "timeout"
            logger.warning("分享审核超 24h,放弃推送:%s(%s)", task.name, task.share_code)
            await bot._notify_uid(
                task.uid, f"⌛ 分享「{task.name}」审核超 24h 未通过,已放弃推送;文件保留在暂存目录。"
            )
            return

        # 卡片链接必须带访问码(建分享时 115 自动生成;此前 URL 拼接漏掉导致丢码)
        task_url = f"https://115.com/s/{task.share_code}" + (
            f"?password={task.receive_code}" if task.receive_code else ""
        )
        link = ParsedLink("115", task.share_code, task_url, task.receive_code or None)
        # 显式查分享审核状态:违规分享的文件列表依然可读(read_share 成功≠审核通过)
        try:
            st = await bot.reader.share_status(task.share_code, task.receive_code or None)
        except Exception as exc:  # noqa: BLE001 - 状态查询失败按继续等待处理
            logger.warning("分享状态查询失败(%s):%s", task.share_code, exc)
            task.attempts += 1
            return
        if st["violating"] or st["expired"]:
            task.status = "violated"
            reason = "违规" if st["violating"] else "已失效"
            logger.warning("分享审核未通过(%s):%s(%s)", reason, task.name, task.share_code)
            await self._move_to("违规", task)
            await bot._notify_uid(
                task.uid, f"🚫 分享「{task.name}」审核未通过({reason}),不予推送;已移入违规目录。"
            )
            return
        if st["auditing"]:
            task.attempts += 1
            logger.info("分享审核中(第 %d 次检查):%s", task.attempts, task.share_code)
            return
        try:
            files = await bot.reader.read_share(link)
        except ShareDead:
            # 失效/违规:不推送,移入违规目录
            task.status = "violated"
            logger.warning("分享审核未通过(失效/违规):%s(%s)", task.name, task.share_code)
            await self._move_to("违规", task)
            await bot._notify_uid(
                task.uid, f"🚫 分享「{task.name}」审核未通过或已失效,不予推送;已移入违规目录。"
            )
            return
        except ShareError as exc:
            # 审核中/快照生成中/限速:正常中间态,继续等
            task.attempts += 1
            logger.info("分享审核中(%s,第 %d 次检查):%s", task.share_code, task.attempts, exc)
            return

        # 审核通过:推卡(与手动推送同链路)
        media = analyze_share(files) or AggregatedMedia(
            title=task.name, file_count=len(files),
            total_size=sum(f.size for f in files if not f.is_dir),
        )
        details = await bot.tmdb.match(media) if bot.tmdb else None
        targets = cfg.channels_for("115")
        if not targets:
            logger.warning("流水线推送中止:未登记 115 归属频道(%s)", task.name)
            return  # 保留 auditing,等用户登记后下轮推送
        title = (details["title"] if details else media.title) or task.name
        # 同归属多个频道逐个都投(与手动/监控链路同一口径,见 bot.push_link)
        sent = 0
        for target in targets:
            try:
                await bot._deliver(media, details, link, files, target)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮其它频道
                logger.error("流水线推送失败(%s → %s):%s", task.name, target, exc, exc_info=exc)
        if not sent:
            task.attempts += 1   # 一条都没送出去:下轮重试(分享码不变,不会重复建)
            return
        bot.store.mark_pushed(task.share_code, title)
        task.status = "done"
        logger.info("流水线推送成功:%s(%s)", task.name, task.share_code)
        await self._move_to(cfg.pipeline_dirs()[1], task)
        label = f"🎬 {title}" + (f" ({details['year']})" if details and details["year"] else "")
        await bot._notify_uid(task.uid, f"✅ 分享「{task.name}」审核通过并已推送({label});已移入已发布目录。")

    async def _move_to(self, sub_name: str, task: PipelineTask) -> None:
        """任务目录移入流水线子目录(已发布/违规);失败仅告警。

        sub_name 为相对流水线根的子目录名("已发布"/"违规");根为 CID 时
        在其下建/取子目录,根为路径时整路径 ensure_dir。
        """
        try:
            root = self.bot.cfg.pipeline_root_dir
            if root.strip().isdigit():
                dest_cid = await self.bot.saver.ensure_dir(sub_name, parent_cid=int(root))
            else:
                dest_cid = await self.bot.saver.ensure_dir(f"{root.rstrip('/')}/{sub_name}")
            await self.bot.reader.fs_move(task.fid, dest_cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("移动任务目录失败(%s → %s):%s", task.name, sub_name, exc)
            return

