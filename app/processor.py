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
from app.links import ParsedLink
from app.media import analyze_share
from app.namer import render_name
from app.pan115 import ShareFile
from app.probe import probe_file

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
              ".rmvb", ".webm", ".mpg", ".mpeg", ".iso", ".m4v"}
# 字幕单独拎出来:取件段清理源目录时按「有没有视频/字幕」判定要不要删,
# 它只认这个子集(不含 .nfo/.jpg 这类伴行美术与元数据)
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub"}
SIDECAR_EXTS = SUBTITLE_EXTS | {".nfo", ".jpg", ".jpeg", ".png"}


class ProcessChain:
    """本地文件处理链;持有 bot 复用其 cfg/store/tmdb/push_link。"""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._loop_task: asyncio.Task | None = None

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
        """扫一期 media/openlist;返回报告摘要(手动 /process 与自动轮共用)。"""
        src = Path(self.bot.cfg.openlist_dir)
        if not src.is_dir():
            return f"处理段:落地点不存在({src})"
        counts: dict[str, int] = {}
        targets = [p for p in sorted(src.rglob("*")) if p.is_file()]
        for path in targets:
            outcome = await self._handle(path)
            counts[outcome] = counts.get(outcome, 0) + 1
        pruned = self._prune_empty_dirs(src)
        total = sum(counts.values())
        if not total:
            return "📦 处理段:落地点没有可处理的文件" + (f"(清理空目录 {pruned} 个)" if pruned else "")
        parts = [f"{k} {v}" for k, v in counts.items() if v]
        tail = f"（共 {total} 个文件" + (f";清理空目录 {pruned} 个)" if pruned else "）")
        return "📦 处理段:" + "、".join(parts) + tail

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

    async def _handle(self, path: Path) -> str:
        """单个文件:守门 → 探测 → 识别 → 重命名 → 哈希 → 推卡 → 归档。"""
        try:
            return await self._process(path)
        except Exception as exc:  # noqa: BLE001 - 单文件异常不影响其余
            logger.error("处理失败(%s):%s", path.name, exc, exc_info=exc)
            self._record(path, status="failed", error=str(exc)[:200])
            return "failed"

    async def _process(self, path: Path) -> str:
        original_name = path.name     # 改名后清理旧名记录用
        src_stem = path.stem          # 伴行匹配用(视频改名后 stem 会变)
        if path.suffix.lower() not in VIDEO_EXTS:
            return "非视频"                        # 伴行文件不动,等视频一起走
        if not self._gate(path):
            return "未就绪"

        row = self.bot.store.get_local_file(path.name, path.stat().st_size)
        if row and row["status"] in ("processed", "unrecognized", "pushing"):
            return "已处理"

        probe = await probe_file(str(path))
        if not probe.ok():
            logger.warning("探测不到媒体信息,按未识别拦下:%s", path.name)
            self._record(path, status="unrecognized", error="ffprobe 无有效媒体流")
            await self._notify(f"❓ 无法处理「{path.name}」:探测不到有效媒体流,已留在落地点待人工。")
            return "unrecognized"

        # ③ 清洗(按需,在重命名之前):只有探到广告类脏数据才 remux;干净则原样通过
        if self.bot.cfg.clean_enabled:
            try:
                cleaned, why = await clean_file(path)
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
        details = await self.bot.tmdb.match(media) if self.bot.tmdb else None
        if not details:
            # 与转存流水线同口径:TMDB 未命中即"未识别" → 不重命名不推卡,留原地交人工
            logger.warning("TMDB 未命中,拦下:%s", path.name)
            self._record(path, status="unrecognized", error="TMDB 未命中")
            await self._notify(
                f"❓ 无法处理「{path.name}」:TMDB 未命中(识别不出是哪部作品),已留在落地点待人工。")
            return "unrecognized"
        target = render_name(media, details, probe, path.suffix.lower(), raw_name=path.name)
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
        size, root = await ed2k_hash_file(str(final))
        uri = ed2k_uri(final.name, size, root)
        logger.info("处理段 ed2k:%s (%sB, %s…)", final.name, f"{size:,}", root[:16])

        # ③ 推卡(与手动 ed2k 推送同一条链路;按文件 hash 去重)
        link = ParsedLink("ed2k", uri, uri, file_hash=root)
        if self.bot.store.is_pushed(link.key):
            logger.info("处理段跳过推卡(已推送过):%s", final.name)
        else:
            result = await self.bot.push_link(link)
            if not result.ok:
                # 推送失败:文件已改名,记录失败但不移走(下轮按"未推送"重试)
                self._record(final, status="failed", error=result.text[:200], ed2k=uri)
                return "failed"

        # ④ 归档到 clouddrive(含伴行),等上传段接手
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
    def _gate(self, path: Path) -> bool:
        """体积下限 + mtime 静默年龄:宁等一轮,不处理半截文件。"""
        cfg = self.bot.cfg
        try:
            st = path.stat()
        except OSError:
            return False
        if st.st_size < int(cfg.min_size_mb) * 1024 * 1024:
            logger.info("处理段跳过(小于 %dMB):%s", cfg.min_size_mb, path.name)
            return False
        age = time.time() - st.st_mtime
        if age < int(cfg.min_age_seconds):
            logger.info("处理段跳过(写入后仅 %.0f 秒,未到 %d 秒):%s",
                        age, cfg.min_age_seconds, path.name)
            return False
        return True

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
