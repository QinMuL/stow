"""处理段:media/openlist 里的新文件 → 探测 → 重命名 → ed2k → 推卡 → 移入 clouddrive。

链路(用户设计:获取段自动、处理段也自动):
  ⓪ 展开:落地点里的**目录会被递归展开**——目录里每个视频各自独立走整条链
     (用户 2026-09-11 定:季包/合集丢进来就自动逐集处理;归档到上传源时**打平**不保留层级)
  ① 守门:体积下限 + mtime 年龄(避免处理还在写入的半截文件)
  ② 探测(ffprobe)取画质标签——分辨率/编码/音频以**探测为准**,不信原始文件名
  ③ 清洗(ffmpeg remux,**只在探测到广告类脏数据时做**,干净文件一个字节都不动):
     容器全局 tags / 垃圾章节 / 广告音轨字幕轨;清洗在**重命名之前**
  ③ 识别:analyze_share + tmdb.match(**与推卡/目录标准化同一真源**);
     识别不出 → 拦下不推、登记待人工(不猜、不硬走,用户明确要求)
  ④ 重命名(本地文件,不走网盘 API):`片名.年份.SxxEyy.第NN集.画质 {tmdb-NNN}.mkv`
  ⑤ ed2k:MD4 分块哈希 → 生成链接(文件名用规范名)
  ⑥ 推卡:bot.push_link(ed2k) → ed2k 归属频道;按文件 hash 去重(与手动推送同一张卡)
  ⑦ 移入 media/clouddrive(含同名字幕/图片伴行),等上传段接手

失败处理:文件留在原地 + 记错误 + 私聊通知;绝不会产生"半成品对外产出"。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from app.cleaner import CleanError, clean_file
from app.ed2k import ed2k_hash_file, ed2k_uri
from app.kicker import Kicker
from app.links import ParsedLink
from app.media import analyze_share, get_quality_info
from app.namer import render_name
from app.pan115 import ShareFile, strip_dup_suffix
from app.probe import probe_file

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
              ".rmvb", ".webm", ".mpg", ".mpeg", ".iso", ".m4v"}
# 字幕单独拎出来:取件段清理源目录时按「有没有视频/字幕」判定要不要删,
# 它只认这个子集(不含 .nfo/.jpg 这类伴行美术与元数据)
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub"}
SIDECAR_EXTS = SUBTITLE_EXTS | {".nfo", ".jpg", ".jpeg", ".png"}

_GATE_NEVER = float("inf")   # 守门返回它 = 一直不符合(如小于体积下限),不必"等一会再看"
_BUSY_RETRY_SECONDS = 5.0    # 撞上"已有扫描在跑"时,重排一次触发(不丢通知)


class ProcessChain:
    """本地文件处理链;持有 bot 复用其 cfg/store/tmdb/push_link。"""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._loop_task: asyncio.Task | None = None
        # 通知式衔接:上一段(获取)干完可以立刻踢我们一轮;我们归档完也可以踢下一段
        self.kicker = Kicker(self.scan_now, name="处理段")
        self.on_done = None                    # 归档成功后的回调(bot 装配时接上传段)
        self._retry_after: float | None = None  # 本轮里"最短还要等多久"(用于排延迟触发)
        # 整轮互斥:四个入口(定时轮/Kicker/手动/Web)都跑在这一个事件循环上,协程会在
        # await 处交错 —— 没有这道闸,同一文件会被两轮同时处理(见 scan_now 注释)
        self._scan_lock = asyncio.Lock()
        # 当前正在处理的文件与阶段(总览页显示真进度用;只读内存字段,跨线程读安全)
        self._current: dict = {}

    # ── 生命周期 ────────────────────────────────────────────
    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self.run_loop())

    async def run_loop(self) -> None:
        interval = max(60, int(self.bot.cfg.process_interval_minutes) * 60)
        logger.info("处理段已启动:扫 %s,每 %d 分钟一轮(体积下限 %dMB,静默 %d 秒)",
                    self.bot.cfg.openlist_dir, self.bot.cfg.process_interval_minutes,
                    self.bot.cfg.min_size_mb, self.bot.cfg.min_age_seconds)
        while True:
            try:
                await self.scan_now()
            except Exception as exc:  # noqa: BLE001 - 单轮异常不拖垮循环
                logger.error("处理段本轮异常:%s", exc, exc_info=exc)
            await asyncio.sleep(interval)

    # ── 一轮 ────────────────────────────────────────────────
    async def scan_now(self) -> str:
        """扫一期落地点;**同一时刻只允许一轮在跑**(四个入口共用)。

        四个入口(5 分钟定时轮 / Kicker 通知式 / 手动 `/process` / Web「跑一轮」)都跑在
        同一个事件循环上,两个协程会在 await 处交错 —— Kicker 只保证"它自己触发的轮次不
        重叠",管不住另外三个。实测(2026-09-12)时序:第一轮已改名并卡在哈希,第二轮此
        时扫描,看到的是改名后的文件(没有对应搬运记录 + mtime 够老 → 守门放行)→ 重复
        哈希/推卡;第一轮把文件移走后,第二轮 FileNotFoundError 并写进失败记录。
        让位的那一轮**重排一次**,别把这次通知吞掉。
        """
        if self._scan_lock.locked():
            logger.info("处理段已有扫描在跑,本轮让位(重排 %.0f 秒后)", _BUSY_RETRY_SECONDS)
            self.kicker.kick(delay=_BUSY_RETRY_SECONDS)
            return "跳过:已有扫描在跑"
        async with self._scan_lock:
            return await self._scan_once()

    async def _scan_once(self) -> str:
        """扫一期 media/openlist;返回报告摘要(手动 /process 与自动轮共用)。"""
        src = Path(self.bot.cfg.openlist_dir)
        if not src.is_dir():
            return f"处理段:落地点不存在({src})"
        counts: dict[str, int] = {}
        self._retry_after = None
        targets = [p for p in sorted(src.rglob("*")) if p.is_file()]
        present: set[tuple[str, int]] = set()
        for p in targets:
            try:
                present.add((p.name, p.stat().st_size))
            except OSError:
                continue
        for path in targets:
            outcome = await self._handle(path)
            counts[outcome] = counts.get(outcome, 0) + 1
        swept = self._reconcile_records(present)
        pruned = self._prune_empty_dirs(src)
        # 通知式衔接:本轮归档了东西 → 立刻让上传段看一轮(不等它自己的 5 分钟轮询)
        if counts.get("processed") and self.on_done is not None:
            self.on_done()
        # 有文件"还差一会儿"才到守门门槛 → 按最短的剩余时间排一次延迟触发
        if self._retry_after is not None:
            self.kicker.kick(delay=self._retry_after)
        total = sum(counts.values())
        tail_bits = []
        if swept:
            tail_bits.append(f"收尾陈旧记录 {swept} 条")
        if pruned:
            tail_bits.append(f"清理空目录 {pruned} 个")
        tail = ("(" + ";".join(tail_bits) + ")") if tail_bits else ""
        if not total:
            return "📦 处理段:落地点没有可处理的文件" + tail
        parts = [f"{k} {v}" for k, v in counts.items() if v]
        return "📦 处理段:" + "、".join(parts) + f"(共 {total} 个文件){tail}"

    def _prune_empty_dirs(self, root: Path) -> int:
        """清掉展开后留下的空目录(自底向上,不碰根)。"""
        removed = 0
        for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    removed += 1
            except OSError as exc:  # noqa: PERF203
                logger.debug("清空目录失败(%s):%s", d, exc)
        return removed

    def _reconcile_records(self, present: set[tuple[str, int]]) -> int:
        """收尾陈旧记录:**非 processed 但文件已不在落地点** → 标 `gone`(记录自愈)。

        这类残影(你改名后跑通、人工删掉、被别的工具移走)如果留着,会**两边口径打架**:
        它**不在待人工清单里**(清单按"文件还在不在"过滤),却**被计数算进去** ——
        于是"待人工 1"永远消不掉,健康判据也跟着一直挂"处理待人工"降级。
        自愈之后计数/清单/判据一起干净,重启重扫也不会再冒出来。
        判据用「文件名 + 大小」:只比名字会被别处的同名文件遮蔽。
        """
        swept = 0
        for row in self.bot.store.list_local_files():
            if row["status"] in ("processed", "gone"):
                continue
            key = (row["name"], int(row["size"] or 0))
            if key in present:
                continue
            self.bot.store.save_local_file(
                name=row["name"], size=key[1], status="gone",
                error="文件已不在落地点(改名/人工删除/外部移走),记录自动收尾")
            logger.info("处理段:收尾陈旧记录(文件已不在落地点):%s", row["name"])
            swept += 1
        if swept:
            logger.info("处理段:本轮收尾 %d 条陈旧记录", swept)
        return swept

    async def _handle(self, path: Path) -> str:
        """单个文件:守门 → 探测 → 识别 → 重命名 → 哈希 → 推卡 → 归档。"""
        try:
            return await self._process(path)
        except Exception as exc:  # noqa: BLE001 - 单文件异常不影响其余
            logger.error("处理失败(%s):%s", path.name, exc, exc_info=exc)
            self._record(path, status="failed", error=str(exc)[:200])
            return "failed"
        finally:
            if self._current.get("name") == path.name:
                self._current = {}          # 收尾:别让页面以为它还在处理

    async def _process(self, path: Path) -> str:
        original_name = path.name     # 改名后清理旧名记录用
        # 重复下载后缀("片名.mkv (1)")会让 suffix 变成 ".mkv (1)":判定与改名都要
        # 用剥掉后缀的名字,否则整集被判成"非视频"永远跳过
        clean = Path(strip_dup_suffix(path.name))
        src_stem = clean.stem         # 伴行匹配用(视频改名后 stem 会变)
        if clean.suffix.lower() not in VIDEO_EXTS:
            return "非视频"                        # 伴行文件不动,等视频一起走
        wait = self._gate(path)
        if wait is not None:
            if wait != _GATE_NEVER and wait < _GATE_NEVER:
                # 记下"最短还要等多久":本轮结束按这个排一次延迟触发,而不是干等下一轮轮询
                self._retry_after = wait if self._retry_after is None else min(self._retry_after, wait)
            return "未就绪"

        row = self.bot.store.get_local_file(path.name, path.stat().st_size)
        if row and row["status"] in ("processed", "unrecognized", "pushing"):
            return "已处理"

        self._phase("探测中", path)
        probe = await probe_file(str(path))
        if not probe.ok():
            logger.warning("探测不到媒体信息,按未识别拦下:%s", path.name)
            self._record(path, status="unrecognized", error="ffprobe 无有效媒体流")
            await self._notify(f"❓ 无法处理「{path.name}」:探测不到有效媒体流,已留在落地点待人工。")
            return "unrecognized"

        # ③ 清洗(按需,在重命名之前):只有探到广告类脏数据才 remux;干净则原样通过
        if self.bot.cfg.clean_enabled:
            try:
                self._phase("清洗中", path, 0.0)
                cleaned, why = await clean_file(
                    path, on_progress=lambda pct: self._phase("清洗中", path, pct))
                if cleaned:
                    logger.info("处理段清洗:%s(%s)", path.name, why)
            except CleanError as exc:
                logger.warning("处理段清洗失败(原件保留,下轮重试):%s:%s", path.name, exc)
                self._record(path, status="failed", error=f"清洗失败:{exc}"[:200])
                await self._notify(
                    f"⚠️ 清洗失败「{path.name}」:{exc}\n原件未动,稍后自动重试。")
                return "failed"

        media = analyze_share([ShareFile(path.name, path.stat().st_size, False)])
        if media is None:
            self._record(path, status="unrecognized", error="文件名无法解析")
            await self._notify(f"❓ 无法处理「{path.name}」:文件名解析不出资源信息,已留原地待人工。")
            return "unrecognized"
        self._phase("识别中", path)
        details = await self.bot.tmdb.match(media) if self.bot.tmdb else None
        if not details:
            # 与转存流水线同口径:TMDB 未命中即"未识别" → 不重命名不推卡,留原地交人工
            logger.warning("TMDB 未命中,拦下:%s", path.name)
            self._record(path, status="unrecognized", error="TMDB 未命中")
            await self._notify(
                f"❓ 无法处理「{path.name}」:TMDB 未命中(识别不出是哪部作品),已留在落地点待人工。")
            return "unrecognized"
        self._phase("重命名", path)
        target = render_name(media, details, probe, clean.suffix.lower(), raw_name=path.name)
        if not target:
            logger.warning("识别不完整(TMDB=%s),拦下:%s", bool(details), path.name)
            self._record(path, status="unrecognized", error="TMDB 未命中或缺季集号")
            await self._notify(
                f"❓ 无法处理「{path.name}」:TMDB 未命中或缺季集号,已留在落地点待人工处理。")
            return "unrecognized"

        # ① 重命名(本地,先落规范名,后续哈希与卡片都用它)
        final = path.with_name(target)
        if final != path:
            if final.exists():
                await self._report_failure(
                    path, f"⚠️ 处理失败「{path.name}」:目标名已存在 {target}")
                return "failed"
            try:
                path.rename(final)
            except PermissionError as exc:
                # 文件仍被写入/被系统占用(Windows 上 CD2 索引、下载器未完全关闭等):
                # 这是**暂时**状态,不当失败处理——否则几次重试就把配额烧光了
                logger.info("处理段改名被占用,下轮再试:%s(%s)", path.name, exc)
                return "未就绪"
            logger.info("处理段重命名:%s → %s", path.name, target)

        # ② ed2k 哈希 + 链接
        self._phase("哈希中", path, 0.0)
        size, root = await ed2k_hash_file(
            str(final), on_progress=lambda pct: self._phase("哈希中", path, pct))
        uri = ed2k_uri(final.name, size, root)
        logger.info("处理段 ed2k:%s (%sB, %s…)", final.name, f"{size:,}", root[:16])

        # ③ 推卡(与手动 ed2k 推送同一条链路;按文件 hash 去重)
        link = ParsedLink("ed2k", uri, uri, file_hash=root)
        if self.bot.store.is_pushed(link.key):
            logger.info("处理段跳过推卡(已推送过):%s", final.name)
        else:
            self._phase("推卡中", path)
            # 把已算好的 media/details 传下去:省一次 TMDB 搜索,也避免重解析我们自己刚改的名字
            result = await self.bot.push_link(
                link, media=media, details=details,
                files=[ShareFile(final.name, final.stat().st_size, False)],
                # 画质行按**改名后**的名字解析:那里才有 ffprobe 实测的分辨率/HDR/编码/
                # 色深/帧率/音频(源文件名常常只有 WEB-DL 一个词,直接用它画质行会退化)
                quality_info=get_quality_info(final.name),
            )
            if not result.ok:
                # 推送失败:文件已改名,记录失败但不移走(下轮按"未推送"重试)
                self._record(final, status="failed", error=result.text[:200], ed2k=uri)
                return "failed"

        # ④ 归档到 clouddrive(含伴行),等上传段接手
        self._phase("归档中", path)
        moved = self._move_to_clouddrive(final, stems=[src_stem])
        self._record(moved, status="processed", ed2k=uri,
                     tmdb_id=(details or {}).get("tmdb_id") or media.tmdb_id)
        # 改过名的话,清掉"原文件名"下的陈旧记录(失败后重试成功会留下孤儿失败行,
        # 否则总览一直挂着"需要你处理"的误报)
        if final.name != original_name:
            removed = self.bot.store.delete_local_file(original_name)
            if removed:
                logger.info("处理段:已清理旧名的陈旧记录(%s)", original_name)
        logger.info("处理段完成:%s → clouddrive(待上传)", moved.name)
        return "processed"

    # ── 辅助 ────────────────────────────────────────────────
    def current_snapshot(self) -> dict:
        """当前正在处理的文件与阶段(没有则空表)。供总览页显示"处理中 · 哈希中 62%"。"""
        return dict(self._current)

    def _phase(self, phase: str, path: Path, percent: float | None = None) -> None:
        """上报"正在做什么"。percent 给了就带百分比(哈希/清洗有真进度,其余只有阶段名)。"""
        text = phase if percent is None else f"{phase} {percent:.0f}%"
        self._current = {"name": path.name, "phase": phase,
                         "percent": round(float(percent or 0.0), 1), "note": f"处理中 · {text}"}

    def _rel_to_landing(self, path: Path) -> str:
        """落地点内的**相对路径**(落地保持子目录结构,所以它与获取段记录的尾部同名)。"""
        try:
            return path.relative_to(self.bot.cfg.openlist_dir).as_posix()
        except ValueError:
            return ""

    def _gate(self, path: Path) -> float | None:
        """分级守门(2026-09-12 用户确认):用**事实**判定,而不是只靠"等了多久"。

        | 文件情况 | 判据 | 结论 |
        |---|---|---|
        | 还在搬运中 | `fetch_state` 状态是 `moving` | **不处理**(卡住 10 分钟也不碰) |
        | 搬运完成 + 大小一致 | 状态 `done`/`cleanup` 且 `src_size` == 本地大小 | **直接放行** |
        | 搬运完成但大小不符 | 同上但大小不等 | 不处理(还没写完/被截断),稍后再看 |
        | 无搬运记录 | 手动拷进落地点/别的工具写的 | 回到时间近似,门槛 `min_age_seconds` |

        返回:None = 放行;秒数 = "还要等这么久"(`_GATE_NEVER` = 一直不符合)。
        """
        cfg = self.bot.cfg
        try:
            st = path.stat()
        except OSError:
            return 1.0
        if st.st_size < int(cfg.min_size_mb) * 1024 * 1024:
            logger.info("处理段跳过(小于 %dMB):%s", cfg.min_size_mb, path.name)
            return _GATE_NEVER

        row = self.bot.store.get_fetch_by_relpath(self._rel_to_landing(path))
        if row is not None:
            status = str(row["status"])
            if status == "moving":
                logger.debug("处理段等待(获取段还在搬运):%s", path.name)
                return 5.0                      # 还在搬:稍后再看(不必等一轮)
            if status in ("done", "cleanup"):
                if int(row["src_size"] or 0) == st.st_size:
                    return None                 # ✅ 搬运完成 + 大小一致 = 完整文件
                logger.info("处理段等待(搬运已完成但大小还对不上:%s 本地 %d ≠ 源 %s):%s",
                            path.name, st.st_size, row["src_size"], path.name)
                return 5.0

        # 没有搬运记录(或记录是 failed 等未确认状态)→ 只能靠时间近似;
        # 门槛用 min_age_seconds(默认 120):不再有"反正要等 5 分钟轮询"可以蹭
        age = time.time() - st.st_mtime
        need = int(cfg.min_age_seconds) - age
        if need > 0:
            logger.info("处理段跳过(无搬运记录,写入后仅 %.0f 秒,未到 %d 秒):%s",
                        age, cfg.min_age_seconds, path.name)
            return need
        return None

    def _move_to_clouddrive(self, path: Path, *, stems: list[str] | None = None) -> Path:
        """视频 + 字幕/图片伴行一起移入 clouddrive(CD2 上传源)。

        伴行按 **stem 前缀**匹配(而非全等):`...S01E12.zh.srt`、`...-poster.jpg` 这类
        都带后缀标记,按新名全等匹配会漏(视频改名后 stem 已变);
        命中后**随视频新名同步改名**(`<新stem>.zh.srt`),否则播放器/Emby 认不出这对字幕。
        """
        dest_dir = Path(self.bot.cfg.clouddrive_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        prefixes = tuple(x for x in ([path.stem, *(stems or [])]) if x)
        siblings = sorted(path.parent.iterdir())
        shutil.move(str(path), str(dest))
        for side in siblings:
            if not side.is_file() or side.suffix.lower() not in SIDECAR_EXTS:
                continue
            matched = next((x for x in prefixes if side.name.startswith(x)), None)
            if matched is None:
                continue
            extra = side.name[len(matched):]           # 如 ".zh.srt" / ".srt"
            new_name = f"{path.stem}{extra}"           # 跟随视频新名
            shutil.move(str(side), str(dest_dir / new_name))
            logger.info("处理段伴行文件一并归档:%s → %s", side.name, new_name)
        return dest

    def _record(self, path: Path, *, status: str, error: str = "",
                ed2k: str = "", tmdb_id: int | None = None) -> None:
        try:
            self.bot.store.save_local_file(
                name=path.name, size=path.stat().st_size, status=status,
                error=error, ed2k=ed2k, tmdb_id=tmdb_id)
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响主流程
            logger.warning("处理段记账失败(%s):%s", path.name, exc)

    async def _report_failure(self, path: Path, text: str) -> None:
        """记 failed 并通知;通知**仅首次转为失败**时发(反复失败不刷屏,日志照记)。

        必须先读旧状态再落库,否则读到的永远是刚写进去的 failed。
        """
        prev = (self.bot.store.get_local_file(path.name) or {}).get("status")
        self._record(path, status="failed", error=text[:200])
        if prev != "failed":
            await self._notify(text)

    async def _notify(self, text: str) -> None:
        if self.bot.cfg.tg_admin_ids:
            await self.bot._notify_uid(self.bot.cfg.tg_admin_ids[0], text)
